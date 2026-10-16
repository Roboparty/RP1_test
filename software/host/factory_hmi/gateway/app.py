"""FastAPI control gateway for the RP1 factory aging-test HMI."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import sqlite3
import threading
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse

from factory_hmi.plc.service import PlcCabinetService, PlcControlDisabledError
from factory_hmi.plc.coordination import PlcRequestConflict
from factory_hmi.sync.outbox import Outbox

from .lease import ControlLease, LeaseConflict
from .schemas import (
    BenchCodeRequest,
    CanConnectRequest,
    ConfigPreviewRequest,
    ConfigureRequest,
    EnableMotorsRequest,
    ExecutionActionRequest,
    HistoryImportRequest,
    ManualMoveRequest,
    PlatformLoginRequest,
    PlaybackStartRequest,
    PlcChannelRequest,
    PlcOperatorRequest,
    PlcSetpointRequest,
    PlcStartRequest,
    TrajectoryImportRequest,
    ZeroConfirmRequest,
    ZeroMotorRequest,
    ZeroPrepareRequest,
)


def _default_controller(
    data_root: Path,
    outbox: Outbox,
    report_root: Path | None = None,
) -> Any:
    from factory_hmi.core.service import FactoryService

    return FactoryService(
        data_root=data_root,
        report_root=report_root,
        outbox=outbox,
    )


def _json_result(result: Any) -> Any:
    if hasattr(result, "to_dict") and callable(result.to_dict):
        return result.to_dict()
    if isinstance(result, Path):
        return str(result)
    return result


class _PlatformCoordinator:
    """Own the one uploader allowed to consume the gateway-wide Outbox."""

    def __init__(
        self,
        outbox: Outbox,
        *,
        snapshot_provider: Callable[[], dict[str, Any]] | None = None,
        on_execution: Callable[[str, str], None] | None = None,
    ) -> None:
        self.outbox = outbox
        self._lock = threading.RLock()
        self._uploader: Any | None = None
        self._identity = ""
        self._snapshot_provider = snapshot_provider
        self._on_execution = on_execution
        self._mode = (
            os.environ.get("RP1_FACTORY_PLATFORM_MODE", "reliability_v1")
            .strip()
            .lower()
        )
        if self._mode not in {"legacy", "reliability_v1"}:
            raise ValueError(
                "RP1_FACTORY_PLATFORM_MODE must be legacy or reliability_v1"
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            presence = getattr(self._uploader, "presence", None)
            return {
                "operator": self._identity,
                "uploader_running": self._uploader is not None,
                "mode": self._mode,
                "presence": presence.status() if presence else None,
            }

    def submit(self, operation: Callable[[], Any], test_id: str) -> Any:
        with self._lock:
            if self._mode != "reliability_v1" or not self._identity:
                raise ValueError("请先登录当前平台再核实归档测试数据。")
            existing = self.outbox.get_by_test_id(test_id)
            if existing and existing.submission_owner and existing.submission_owner != self._identity:
                raise ValueError("此记录已绑定其他提交账号，请使用原账号处理。")
            result = operation()
            if isinstance(result, dict) and result.get("execution_uuid"):
                self.outbox.update(result["execution_uuid"], submission_owner=self._identity)
                result = self.outbox.get(result["execution_uuid"]).to_dict()
            self.wake()
            return result

    def options(self) -> dict[str, Any]:
        from factory_hmi.sync.reliability_platform import load_test_profiles
        from factory_hmi.sync.uploader import PlatformError

        path = os.environ.get("RP1_RELIABILITY_PROFILES", "").strip() or None
        yaml_profiles = load_test_profiles(path)
        live: list[dict[str, Any]] = []
        context_error = ""
        stations: list[dict[str, Any]] = []
        with self._lock:
            uploader = self._uploader
            identity = self._identity
        if uploader is not None and identity:
            try:
                live = uploader.context_profiles()
                stations = uploader.station_options()
            except PlatformError as exc:
                context_error = str(exc)
        live_keys = {str(item.get("key") or "") for item in live}
        profiles = live + [
            item for item in yaml_profiles if str(item.get("key") or "") not in live_keys
        ]
        return {
            "mode": self._mode,
            "profiles": profiles,
            "context_error": context_error,
            "stations": stations,
        }

    def set_presence(self, payload: dict[str, Any]) -> None:
        with self._lock:
            uploader = self._uploader
        if uploader is not None:
            uploader.set_presence(payload)

    def login(self, request: PlatformLoginRequest) -> dict[str, Any]:
        if self._mode == "reliability_v1":
            from factory_hmi.sync.reliability_platform import (
                reliability_worker_from_environment,
            )

            with self._lock:
                if self._uploader is None:
                    self.outbox.recover_interrupted()
                    self._uploader = reliability_worker_from_environment(
                        self.outbox,
                        snapshot_provider=self._snapshot_provider,
                        on_execution=self._on_execution,
                    )
                grant = self._uploader.authenticate(
                    request.username,
                    request.password,
                )
                self._uploader.start()
                self._identity = str((grant.get("user") or {}).get("username") or request.username).strip()
                self._uploader.wake()
            return {
                "operator": self._identity,
                "station_id": request.station_id or "",
                "status": "connected",
                "mode": self._mode,
                "expires_at": grant.get("expires_at", ""),
            }

        from factory_hmi.sync.uploader import (
            PlatformClient,
            UploaderWorker,
            read_secret,
        )

        station_id = (
            request.station_id or os.environ.get("RP1_FACTORY_STATION_ID", "").strip()
        )
        if not station_id:
            raise RuntimeError("RP1_FACTORY_STATION_ID is not configured")
        token_file = os.environ.get("RP1_FACTORY_MACHINE_TOKEN_FILE", "").strip()
        machine_token = ""
        if token_file:
            try:
                machine_token = read_secret(token_file)
            except FileNotFoundError:
                machine_token = ""
        if not machine_token:
            machine_token = os.environ.get("RP1_FACTORY_MACHINE_TOKEN", "").strip()
        # Local lab may disable platform machine-token checks; keep a non-empty
        # placeholder so the uploader client can still authenticate with
        # station_id + operator credentials alone.
        if not machine_token:
            machine_token = "local-dev-machine-token-not-required"

        with self._lock:
            if self._uploader is not None and self._uploader.station_id != station_id:
                if self._identity:
                    raise RuntimeError(
                        "platform uploader is already logged in for "
                        f"station {self._uploader.station_id}"
                    )
                self._uploader.stop()
                self._uploader = None
            if self._uploader is None:
                self.outbox.recover_interrupted()
                self._uploader = UploaderWorker(
                    self.outbox,
                    PlatformClient(
                        os.environ.get(
                            "RP1_FACTORY_PLATFORM_URL",
                            "http://127.0.0.1:8080",
                        )
                    ),
                    station_id=station_id,
                    machine_token=machine_token,
                )
            grant = self._uploader.authenticate(
                request.username,
                request.password,
            )
            self._uploader.start()
            self._identity = request.username.strip()
            self._uploader.wake()
        return {
            "operator": self._identity,
            "station_id": station_id,
            "status": "connected",
            "mode": self._mode,
            "expires_at": grant.get("expires_at", ""),
        }

    def logout(self) -> dict[str, Any]:
        with self._lock:
            if self._uploader is not None:
                self._uploader.clear_credentials()
            self._identity = ""
        return {"operator": "", "status": "logged_out"}

    def wake(self) -> None:
        with self._lock:
            uploader = self._uploader
        if uploader is not None:
            uploader.wake()

    def close(self) -> None:
        with self._lock:
            uploader = self._uploader
            self._uploader = None
            self._identity = ""
        if uploader is not None:
            uploader.stop()
            uploader.clear_credentials()


class GatewayRuntime:
    def __init__(
        self,
        controller: Any,
        *,
        lease_timeout_s: float = 2.0,
        status_rate_hz: float = 10.0,
        data_root: Path | None = None,
        outbox: Outbox,
        platform: _PlatformCoordinator,
        expiry_callback: Callable[[], None] | None = None,
    ) -> None:
        self.controller = controller
        self.lease = ControlLease(lease_timeout_s)
        self.status_rate_hz = float(status_rate_hz)
        self.repo_root = Path(__file__).resolve().parents[2]
        self.data_root = (
            (
                data_root
                or Path(
                    os.environ.get(
                        "RP1_FACTORY_DATA_ROOT",
                        "/var/lib/rp1-factory-hmi",
                    )
                )
            )
            .expanduser()
            .resolve()
        )
        self.trajectory_root = self.data_root / "trajectories"
        self.config_root = self.data_root / "configs"
        self._stop = threading.Event()
        self._lease_thread: threading.Thread | None = None
        self._expiry_callback = expiry_callback
        self._lease_guard = threading.RLock()
        self._motion_epoch_guard = threading.Lock()
        self._motion_epoch = 0
        self._motion_interrupts_pending = 0
        self.outbox = outbox
        self._platform = platform

    def start(self) -> None:
        self.trajectory_root.mkdir(parents=True, exist_ok=True)
        self.config_root.mkdir(parents=True, exist_ok=True)
        self._stop.clear()
        self._lease_thread = threading.Thread(
            target=self._monitor_lease,
            name="factory-hmi-lease",
            daemon=True,
        )
        self._lease_thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._lease_thread is not None:
            self._lease_thread.join(timeout=2.0)
        try:
            state = str(self.snapshot().get("state", ""))
            if state not in {"disconnected", "completed"}:
                self._safe_stop("gateway shutdown")
            self._invoke(("disconnect",))
        except Exception:
            pass

    def snapshot(self) -> dict[str, Any]:
        result = self._invoke(("snapshot", "get_snapshot"))
        if hasattr(result, "to_dict"):
            result = result.to_dict()
        if not isinstance(result, dict):
            raise RuntimeError("controller snapshot must be a mapping")
        lease = self.lease.snapshot()
        value = dict(result)
        if value.get("config_path"):
            value["config_path"] = self._config_reference(Path(value["config_path"]))
        station = value.get("station")
        if "state" not in value and isinstance(station, dict):
            value["state"] = station.get("state")
        if "connected" not in value and isinstance(station, dict):
            value["connected"] = bool(station.get("backend_connected", False))
        value["lease"] = {
            "owner": lease.owner,
            "age_s": lease.age_s,
            "timeout_s": lease.timeout_s,
            "expired": lease.expired,
        }
        platform = self._platform.snapshot()
        if platform["uploader_running"]:
            state = str(value.get("state") or "idle")
            platform_state = state
            if platform_state not in {"running", "paused", "fault", "completed"}:
                platform_state = "idle"
            progress = value.get("progress")
            if not isinstance(progress, dict):
                progress = {}
            playback = value.get("playback")
            if not isinstance(playback, dict):
                playback = {}
            station = value.get("station")
            if not isinstance(station, dict):
                station = {}
            self._platform.set_presence(
                {
                    "state": platform_state,
                    "current_test_id": str(playback.get("test_id") or ""),
                    "bench_id": os.environ.get("RP1_FACTORY_BENCH_ID", ""),
                    "sample_id": str(playback.get("robot_id") or ""),
                    "active_seconds": float(progress.get("active_seconds") or 0),
                    "details": {
                        "local_state": state,
                        "operator": platform["operator"],
                        "test_case": str(station.get("limb") or ""),
                        "sync_status": "online",
                    },
                }
            )
        value["platform"] = platform
        return value

    def platform_login(self, request: PlatformLoginRequest) -> dict[str, Any]:
        return self._platform.login(request)

    def platform_logout(self) -> dict[str, Any]:
        return self._platform.logout()

    def submit_execution(
        self,
        test_id: str,
        *,
        retry: bool = False,
        reason: str = "",
        verified_status: str = "",
        operator_summary: str = "",
        review: dict[str, Any] | None = None,
    ) -> Any:
        if retry:
            return self._platform.submit(lambda: self._invoke(("retry_submission",), test_id), test_id)
        return self._platform.submit(
            lambda: self._invoke(
                ("submit_execution",),
                test_id,
                reason,
                verified_status,
                operator_summary,
                review,
            ),
            test_id,
        )

    def configure(self, request: ConfigureRequest) -> Any:
        config_path = self._allowed_config_path(request.config_path)
        bus_bindings = (
            None
            if request.bus_bindings is None
            else [item.model_dump() for item in request.bus_bindings]
        )
        motor_overrides = (
            None
            if request.motor_overrides is None
            else [item.model_dump() for item in request.motor_overrides]
        )
        return self._invoke(
            ("configure",),
            config_path=str(config_path),
            limb=request.limb,
            backend=request.backend,
            bus_bindings=bus_bindings,
            motor_overrides=motor_overrides,
        )

    def preview_config(self, request: ConfigPreviewRequest) -> dict[str, Any]:
        from factory_hmi.core.config import preview_station_config

        config_path = self._allowed_config_path(request.config_path)
        result = preview_station_config(request.limb, config_path)
        result["config_path"] = self._config_reference(config_path)
        return result

    def _config_reference(self, path: Path) -> str:
        """Expose portable YAML references while retaining resolved paths internally."""
        resolved = path.expanduser().resolve()
        if resolved.is_relative_to(self.config_root):
            return "uploaded/" + resolved.relative_to(self.config_root).as_posix()
        configured = os.environ.get("RP1_FACTORY_ALLOWED_CONFIG_ROOT", "").strip()
        roots = (
            tuple(Path(item).expanduser().resolve() for item in configured.split(os.pathsep) if item)
            if configured else (self.repo_root / "scripts" / "config",)
        )
        for root in roots:
            if resolved.is_relative_to(root):
                return resolved.relative_to(root).as_posix()
        return str(path)

    def _allowed_config_path(self, value: str) -> Path:
        uploaded = Path(value).expanduser()
        if not uploaded.is_absolute() and uploaded.parts and uploaded.parts[0] == "uploaded":
            uploaded = self.config_root.joinpath(*uploaded.parts[1:])
        if uploaded.is_absolute():
            try:
                uploaded = uploaded.resolve(strict=True)
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail="配置文件不存在，请选择有效的 YAML 文件或相对配置文件名") from exc
            if (
                uploaded.suffix.lower() in {".yaml", ".yml"}
                and self.config_root in uploaded.parents
            ):
                return uploaded
        return self._allowed_path(
            value,
            environment_name="RP1_FACTORY_ALLOWED_CONFIG_ROOT",
            default_roots=(self.repo_root / "scripts" / "config",),
            suffixes={".yaml", ".yml"},
        )

    def import_trajectory(self, path: str) -> Any:
        uploaded = Path(path).expanduser()
        if uploaded.is_absolute():
            uploaded = uploaded.resolve(strict=True)
        if (
            uploaded.is_absolute()
            and uploaded.suffix.lower() == ".npz"
            and (
                uploaded == self.trajectory_root
                or self.trajectory_root in uploaded.parents
            )
        ):
            trajectory_path = uploaded
        else:
            trajectory_path = self._allowed_path(
                path,
                environment_name="RP1_FACTORY_ALLOWED_TRAJECTORY_ROOT",
                default_roots=(
                    self.repo_root / "motion",
                    self.repo_root / "outputs",
                    self.trajectory_root,
                ),
                suffixes={".npz"},
            )
        return self._invoke(
            ("import_trajectory", "load_trajectory"),
            str(trajectory_path),
        )

    def config_profiles(self) -> list[dict[str, Any]]:
        from factory_hmi.core.config import DEFAULT_CONFIG_PATHS

        configured = os.environ.get(
            "RP1_FACTORY_ALLOWED_CONFIG_ROOT",
            "",
        ).strip()
        configured_roots = (
            tuple(
                Path(item).expanduser().resolve()
                for item in configured.split(os.pathsep)
                if item
            )
            if configured
            else (self.repo_root / "scripts" / "config",)
        )
        roots = (*configured_roots, self.config_root)
        limb_by_path: dict[Path, list[str]] = {}
        for limb, path in DEFAULT_CONFIG_PATHS.items():
            limb_by_path.setdefault(path.resolve(), []).append(limb)
        result: list[dict[str, Any]] = []
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted((*root.glob("*.yaml"), *root.glob("*.yml"))):
                resolved = path.resolve()
                result.append(
                    {
                        "name": path.stem,
                        "path": self._config_reference(resolved),
                        "limbs": sorted(limb_by_path.get(resolved, [])),
                    }
                )
        return result

    def _allowed_path(
        self,
        value: str,
        *,
        environment_name: str,
        default_roots: tuple[Path, ...],
        suffixes: set[str],
    ) -> Path:
        configured = os.environ.get(environment_name, "").strip()
        roots = (
            tuple(
                Path(item).expanduser().resolve()
                for item in configured.split(os.pathsep)
                if item
            )
            if configured
            else tuple(root.resolve() for root in default_roots)
        )
        path = Path(value).expanduser()
        if not path.is_absolute():
            candidates = [
                self.repo_root / path,
                *(root / path for root in roots),
                *(root / path.name for root in roots),
            ]
            path = next(
                (candidate for candidate in candidates if candidate.is_file()),
                candidates[0],
            )
        try:
            path = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="配置文件不存在，请选择有效的 YAML 文件或相对配置文件名") from exc
        if path.suffix.lower() not in suffixes:
            allowed = ", ".join(sorted(suffixes))
            raise ValueError(f"{path.name} must use one of: {allowed}")
        if not any(path == root or root in path.parents for root in roots):
            raise ValueError(
                f"path is outside the configured production library: {path}"
            )
        return path

    def _invoke(self, names: tuple[str, ...], *args: Any, **kwargs: Any) -> Any:
        for name in names:
            method = getattr(self.controller, name, None)
            if callable(method):
                return method(*args, **kwargs)
        raise RuntimeError(f"controller does not implement {'/'.join(names)}")

    def _safe_stop(self, reason: str) -> None:
        try:
            self._invoke(("stop",), reason=reason)
        except TypeError:
            self._invoke(("stop",))
        except Exception:
            try:
                self._invoke(("disable", "emergency_disable"))
            except Exception:
                pass

    def _monitor_lease(self) -> None:
        while not self._stop.wait(0.1):
            with self._lease_guard:
                if not self.lease.expired():
                    continue
                try:
                    state = str(self.snapshot().get("state", ""))
                except Exception:
                    state = ""
                if state in {"armed", "running", "paused", "stopping"}:
                    self._safe_stop("operator lease expired")
                self._safe_release()

    def _safe_release(self) -> None:
        for names in (
            ("disable_motors", "disable", "emergency_disable"),
            ("disconnect",),
            ("can_disconnect",),
        ):
            try:
                self._invoke(names)
            except Exception:
                pass
        self.lease.release()
        if self._expiry_callback is not None:
            self._expiry_callback()


_SAFE_SESSION_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class GatewayHub:
    """Own isolated station runtimes and globally arbitrate CAN interfaces."""

    def __init__(
        self,
        controller: Any | None,
        *,
        lease_timeout_s: float,
        status_rate_hz: float,
        data_root: Path,
    ) -> None:
        self.data_root = data_root
        self.lease_timeout_s = lease_timeout_s
        self.status_rate_hz = status_rate_hz
        self._shared_controller = controller
        controller_outbox = (
            getattr(controller, "outbox", None) if controller is not None else None
        )
        self.outbox = controller_outbox or Outbox(
            self.data_root / "sync" / "outbox.sqlite3",
            recover_interrupted=False,
        )
        self.outbox_migration_errors: list[str] = []
        sessions_root = self.data_root / "sessions"
        if controller_outbox is None and sessions_root.is_dir():
            for legacy_path in sorted(sessions_root.glob("*/sync/outbox.sqlite3")):
                try:
                    self.outbox.import_from(legacy_path)
                except (OSError, sqlite3.DatabaseError, ValueError) as exc:
                    self.outbox_migration_errors.append(f"{legacy_path}: {exc}")
            self._migrate_session_reports(sessions_root)
        self._platform = _PlatformCoordinator(
            self.outbox,
            snapshot_provider=self.presence_snapshot,
            on_execution=self.apply_live_execution_code,
        )
        plc_config = os.environ.get("RP1_FACTORY_PLC_CONFIG", "").strip() or None
        self.plc = PlcCabinetService(
            config_path=plc_config,
            data_root=self.data_root,
        )
        self._sessions: dict[str, GatewayRuntime] = {}
        self._interface_owners: dict[str, str] = {}
        self._last_owner: str | None = None
        self._lock = threading.RLock()
        self._started = False

    def apply_live_execution_code(self, session_key: str, execution_code: str) -> None:
        with self._lock:
            runtime = self._sessions.get(session_key)
        if runtime is None:
            return
        runtime._invoke(("adopt_platform_execution_code",), execution_code)

    def presence_snapshot(self) -> dict[str, Any]:
        from factory_hmi.sync.presence import build_presence_snapshot

        with self._lock:
            runtimes = list(self._sessions.items())
        snapshots = {}
        for key, runtime in runtimes:
            snapshot = runtime._invoke(("snapshot", "get_snapshot"))
            if hasattr(snapshot, "to_dict"):
                snapshot = snapshot.to_dict()
            if not isinstance(snapshot, dict):
                raise RuntimeError("Invalid station snapshot")
            snapshots[key] = snapshot
        return build_presence_snapshot(snapshots, self.plc.snapshot())

    def _migrate_session_reports(self, sessions_root: Path) -> None:
        executions_root = self.data_root / "executions"
        exports_root = self.data_root / "exports"
        executions_root.mkdir(parents=True, exist_ok=True)
        exports_root.mkdir(parents=True, exist_ok=True)
        for legacy_execution in sorted(sessions_root.glob("*/executions/*")):
            if (
                not legacy_execution.is_dir()
                or legacy_execution.is_symlink()
                or not (legacy_execution / "manifest.json").is_file()
            ):
                continue
            destination = executions_root / legacy_execution.name
            if destination.exists():
                continue
            try:
                shutil.copytree(legacy_execution, destination)
            except OSError as exc:
                shutil.rmtree(destination, ignore_errors=True)
                self.outbox_migration_errors.append(f"{legacy_execution}: {exc}")
        for legacy_bundle in sorted(sessions_root.glob("*/exports/*.tar.gz")):
            if not legacy_bundle.is_file() or legacy_bundle.is_symlink():
                continue
            destination = exports_root / legacy_bundle.name
            if destination.exists():
                continue
            try:
                shutil.copy2(legacy_bundle, destination)
            except OSError as exc:
                self.outbox_migration_errors.append(f"{legacy_bundle}: {exc}")

    @property
    def shared(self) -> bool:
        return self._shared_controller is not None

    def start(self) -> None:
        with self._lock:
            self._started = True
            self.plc.start()
            if self.shared:
                self.session("shared")

    def close(self) -> None:
        with self._lock:
            runtimes = list(self._sessions.values())
            self._sessions.clear()
            self._interface_owners.clear()
            self._started = False
        for runtime in runtimes:
            runtime.close()
        self.plc.close()
        self._platform.close()

    def _key(self, owner: str) -> str:
        return "shared" if self.shared else owner

    def session(self, owner: str, *, create: bool = True) -> GatewayRuntime:
        key = self._key(owner)
        with self._lock:
            runtime = self._sessions.get(key)
            if runtime is None and create:
                safe_name = _SAFE_SESSION_NAME.sub("_", key).strip("._") or "anonymous"
                session_root = (
                    self.data_root
                    if self.shared
                    else self.data_root / "sessions" / safe_name
                )
                controller = (
                    self._shared_controller
                    if self.shared
                    else _default_controller(
                        session_root,
                        self.outbox,
                        self.data_root,
                    )
                )
                runtime = GatewayRuntime(
                    controller,
                    lease_timeout_s=self.lease_timeout_s,
                    status_rate_hz=self.status_rate_hz,
                    data_root=session_root,
                    outbox=self.outbox,
                    platform=self._platform,
                    expiry_callback=lambda owner=key: self.release_interfaces(owner),
                )
                self._sessions[key] = runtime
                if self._started:
                    runtime.start()
            if runtime is None:
                raise RuntimeError("station session does not exist")
            self._last_owner = key
            return runtime

    def current(self, owner: str | None = None) -> GatewayRuntime:
        with self._lock:
            if owner:
                return self.session(owner)
            if self._last_owner is not None:
                return self._sessions[self._last_owner]
            return self.session("shared" if self.shared else "anonymous")

    def configure(
        self,
        owner: str,
        runtime: GatewayRuntime,
        request: ConfigureRequest,
    ) -> Any:
        requested = (
            []
            if request.bus_bindings is None
            else [item.interface for item in request.bus_bindings]
        )
        key = self._key(owner)
        previous = self._owned_interfaces(key)
        self._reserve_interfaces(key, requested)
        try:
            result = runtime.configure(request)
            if not requested and isinstance(result, dict):
                interfaces = (result.get("can") or {}).get("interfaces", [])
                requested = [
                    str(item.get("interface"))
                    for item in interfaces
                    if isinstance(item, dict) and item.get("interface")
                ]
                self._reserve_interfaces(key, requested)
            self._set_interfaces(key, requested)
            return result
        except Exception:
            self._set_interfaces(key, previous)
            raise

    def _reserve_interfaces(self, owner: str, interfaces: list[str]) -> None:
        with self._lock:
            conflicts = {
                name: current
                for name in interfaces
                if (current := self._interface_owners.get(name)) not in {None, owner}
            }
            if conflicts:
                details = ", ".join(
                    f"{name} is controlled by {current}"
                    for name, current in sorted(conflicts.items())
                )
                raise LeaseConflict(details)
            for name in interfaces:
                self._interface_owners[name] = owner

    def _owned_interfaces(self, owner: str) -> list[str]:
        with self._lock:
            return [
                name
                for name, current in self._interface_owners.items()
                if current == owner
            ]

    def _set_interfaces(self, owner: str, interfaces: list[str]) -> None:
        with self._lock:
            stale = [
                name
                for name, current in self._interface_owners.items()
                if current == owner
            ]
            for name in stale:
                self._interface_owners.pop(name, None)
            for name in interfaces:
                self._interface_owners[name] = owner

    def release_interfaces(self, owner: str) -> None:
        with self._lock:
            stale = [
                name
                for name, current in self._interface_owners.items()
                if current == owner
            ]
            for name in stale:
                self._interface_owners.pop(name, None)

    def release_session(self, owner: str) -> None:
        """Atomically release a client's control lease and CAN ownership."""
        runtime = self.session(owner, create=False)
        with runtime._lease_guard:
            runtime.lease.release(owner)
            self.release_interfaces(self._key(owner))

    def interface_owners(self) -> dict[str, str]:
        with self._lock:
            return dict(self._interface_owners)


def create_app(
    controller: Any | None = None,
    *,
    lease_timeout_s: float = 2.0,
    status_rate_hz: float = 10.0,
    data_root: Path | None = None,
) -> FastAPI:
    resolved_data_root = (
        (
            data_root
            or Path(
                os.environ.get(
                    "RP1_FACTORY_DATA_ROOT",
                    "/var/lib/rp1-factory-hmi",
                )
            )
        )
        .expanduser()
        .resolve()
    )
    hub = GatewayHub(
        controller,
        lease_timeout_s=lease_timeout_s,
        status_rate_hz=status_rate_hz,
        data_root=resolved_data_root,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        hub.start()
        try:
            yield
        finally:
            hub.close()

    app = FastAPI(
        title="RP1 Factory Aging-Test Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.runtime = hub

    def owner_id(request: Request, explicit: str | None) -> str:
        if explicit and explicit.strip():
            return explicit.strip()
        if request.client is not None:
            return f"host:{request.client.host}"
        return "anonymous"

    def require_token(value: str | None) -> None:
        expected = os.environ.get("RP1_FACTORY_OPERATOR_TOKEN", "").strip()
        if expected and value != expected:
            raise HTTPException(status_code=401, detail="invalid operator token")

    def mutate(
        request: Request,
        client_id: str | None,
        token: str | None,
        operation: Callable[[GatewayRuntime], Any],
        *,
        claim: bool = False,
        preempt_motion: bool = False,
        motion_command: bool = False,
    ) -> dict[str, Any]:
        require_token(token)
        owner = owner_id(request, client_id)
        runtime = hub.session(owner)
        interrupt_registered = False
        try:
            with runtime._motion_epoch_guard:
                motion_epoch = runtime._motion_epoch
            if preempt_motion:
                # Authenticate and validate the lease before signalling the
                # caller's own return operation. Do not wait behind its lease
                # guard merely to request cancellation. Normal command writes
                # remain serialized inside that guard below.
                runtime.lease.refresh(owner)
                with runtime._motion_epoch_guard:
                    runtime._motion_epoch += 1
                    runtime._motion_interrupts_pending += 1
                    interrupt_registered = True
                cancel = getattr(runtime.controller, "request_motion_stop", None)
                if callable(cancel):
                    cancel()
            with runtime._lease_guard:
                with runtime._motion_epoch_guard:
                    if motion_command and (
                        motion_epoch != runtime._motion_epoch
                        or runtime._motion_interrupts_pending
                    ):
                        raise RuntimeError(
                            "queued motor command superseded by disable/disconnect; "
                            "review the current state before sending a new command"
                        )
                if claim:
                    runtime.lease.claim(owner)
                else:
                    runtime.lease.refresh(owner)
                result = operation(runtime)
                runtime.lease.refresh(owner)
            return {
                "ok": True,
                "result": _json_result(result),
                "snapshot": runtime.snapshot(),
            }
        except LeaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            if interrupt_registered:
                with runtime._motion_epoch_guard:
                    runtime._motion_interrupts_pending -= 1

    @app.exception_handler(PlcControlDisabledError)
    async def plc_control_disabled(_request: Request, exc: PlcControlDisabledError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    @app.get("/api/v1/health")
    def health(
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        runtime = (
            hub.session(owner_id(request, x_rp1_client_id))
            if x_rp1_client_id
            else hub.current()
        )
        return {
            "ok": True,
            "service": "rp1-factory-gateway",
            "snapshot": runtime.snapshot(),
            "sessions": len(hub._sessions),
            "outbox": {
                "path": str(hub.outbox.path),
                "migration_errors": list(hub.outbox_migration_errors),
            },
        }

    @app.get("/api/v1/snapshot")
    def snapshot(
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if x_rp1_client_id:
            return hub.session(owner_id(request, x_rp1_client_id)).snapshot()
        return hub.current().snapshot()

    @app.get("/api/v1/plc/snapshot")
    def plc_snapshot() -> dict[str, Any]:
        return hub.plc.snapshot()

    @app.get("/api/v1/platform/options")
    def platform_options(
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        return {"ok": True, "result": hub._platform.options()}

    @app.exception_handler(PlcRequestConflict)
    async def plc_request_conflict(_request: Request, exc: PlcRequestConflict) -> JSONResponse:
        snapshot = await asyncio.to_thread(hub.plc.snapshot)
        return JSONResponse(status_code=409, content={"detail": {
            "code": exc.code, "message": str(exc), "snapshot": snapshot,
        }})

    def plc_mutate(operation: str, body: PlcOperatorRequest, client_id: str | None,
                   token: str | None, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        require_token(token)
        return hub.plc.coordination.execute(
            operation, payload or {}, user=body.user, client_id=client_id,
            request_id=body.request_id, expected_epoch=body.expected_epoch,
            expected_revision=body.expected_revision,
        )

    @app.post("/api/v1/plc/connect")
    def plc_connect(
        body: PlcOperatorRequest,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return plc_mutate("connect", body, x_rp1_client_id, x_rp1_operator_token)

    @app.post("/api/v1/plc/disconnect")
    def plc_disconnect(
        body: PlcOperatorRequest,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return plc_mutate("disconnect", body, x_rp1_client_id, x_rp1_operator_token)

    @app.post("/api/v1/plc/start")
    def plc_start(
        body: PlcStartRequest,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return plc_mutate("start", body, x_rp1_client_id, x_rp1_operator_token, {"channels": body.channels, "voltage": body.voltage, "current": body.current})

    @app.post("/api/v1/plc/stop")
    def plc_stop(
        body: PlcOperatorRequest,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return plc_mutate("stop", body, x_rp1_client_id, x_rp1_operator_token)

    @app.post("/api/v1/plc/all-stop")
    def plc_all_stop(
        body: PlcOperatorRequest,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return plc_mutate("all-stop", body, x_rp1_client_id, x_rp1_operator_token)

    @app.post("/api/v1/plc/reset-fault")
    def plc_reset_fault(
        body: PlcOperatorRequest,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return plc_mutate("reset-fault", body, x_rp1_client_id, x_rp1_operator_token)

    @app.post("/api/v1/plc/channels/{channel}")
    def plc_channel(
        channel: int,
        body: PlcChannelRequest,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        if channel not in {1, 2, 3, 4}:
            raise HTTPException(status_code=422, detail="channel must be in [1, 4]")
        return plc_mutate("channel", body, x_rp1_client_id, x_rp1_operator_token, {"channel": channel, "enabled": body.enabled})

    @app.post("/api/v1/plc/setpoints")
    def plc_setpoints(
        body: PlcSetpointRequest,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return plc_mutate("setpoints", body, x_rp1_client_id, x_rp1_operator_token, {"voltage": body.voltage, "current": body.current})

    @app.get("/api/v1/configs")
    def configs() -> dict[str, Any]:
        return {"items": hub.current().config_profiles()}

    @app.post("/api/v1/config/preview")
    def preview_config(
        body: ConfigPreviewRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = (
            hub.session(owner_id(request, x_rp1_client_id))
            if x_rp1_client_id
            else hub.current()
        )
        return {"ok": True, "result": runtime.preview_config(body)}

    @app.post("/api/v1/config/upload")
    async def upload_config(
        request: Request,
        x_filename: str = Header(),
        x_rp1_limb: str = Header(),
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        filename = Path(x_filename).name
        if Path(filename).suffix.lower() not in {".yaml", ".yml"}:
            raise HTTPException(
                status_code=400,
                detail="motor configuration filename must end with .yaml or .yml",
            )
        body = await request.body()
        if not body or len(body) > 1024 * 1024:
            raise HTTPException(
                status_code=413,
                detail="motor configuration must be 1 byte to 1 MiB",
            )
        runtime.config_root.mkdir(parents=True, exist_ok=True)
        target = runtime.config_root / filename
        temporary = target.with_name(f".{target.stem}.part{target.suffix}")
        temporary.write_bytes(body)
        try:
            result = runtime.preview_config(
                ConfigPreviewRequest(config_path=str(temporary), limb=x_rp1_limb)
            )
        except (ValueError, RuntimeError) as exc:
            temporary.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        temporary.replace(target)
        result["config_path"] = f"uploaded/{target.name}"
        return {"ok": True, "result": result}

    @app.get("/api/v1/can/status")
    def can_status(
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        return {
            "ok": True,
            "result": _json_result(runtime._invoke(("can_status",))),
        }

    @app.get("/api/v1/can/interfaces")
    def can_interfaces(
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = (
            hub.session(owner_id(request, x_rp1_client_id))
            if x_rp1_client_id
            else hub.current()
        )
        result = runtime._invoke(("can_interfaces",))
        if not isinstance(result, dict):
            raise HTTPException(status_code=500, detail="invalid CAN discovery result")
        owners = hub.interface_owners()
        items = []
        for raw in result.get("items", []):
            item = dict(raw) if isinstance(raw, dict) else {}
            item["owner"] = owners.get(str(item.get("interface", "")))
            items.append(item)
        result = dict(result)
        result["items"] = items
        return {"ok": True, "result": result}

    @app.post("/api/v1/configure")
    def configure(
        body: ConfigureRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: hub.configure(
                owner_id(request, x_rp1_client_id),
                runtime,
                body,
            ),
            claim=True,
        )

    def simple_route(
        path: str,
        method_names: tuple[str, ...],
        *,
        release: bool = False,
        preempt_motion: bool = False,
    ) -> None:
        def endpoint(
            request: Request,
            x_rp1_client_id: str | None = Header(default=None),
            x_rp1_operator_token: str | None = Header(default=None),
        ) -> dict[str, Any]:
            owner = owner_id(request, x_rp1_client_id)
            response = mutate(
                request,
                x_rp1_client_id,
                x_rp1_operator_token,
                lambda runtime: runtime._invoke(method_names),
                preempt_motion=preempt_motion,
                motion_command=not preempt_motion,
            )
            if release:
                hub.release_session(owner)
            elif (
                "can_disconnect" in method_names
                or "clear_configuration" in method_names
            ):
                hub.release_interfaces(hub._key(owner))
            return response

        app.post(path, name=path.rsplit("/", 1)[-1])(endpoint)

    simple_route("/api/v1/connect", ("connect",))
    simple_route(
        "/api/v1/configuration/clear",
        ("clear_configuration",),
        release=True,
    )
    simple_route("/api/v1/can/disconnect", ("can_disconnect",))
    simple_route("/api/v1/can/recover", ("can_recover",))
    simple_route("/api/v1/motors/discover", ("discover_motors",))
    simple_route("/api/v1/motors/reset-fault", ("reset_motor_fault",))
    simple_route("/api/v1/motors/disable", ("disable_motors", "disable"), preempt_motion=True)
    simple_route("/api/v1/disconnect", ("disconnect",), release=True, preempt_motion=True)
    simple_route("/api/v1/arm", ("arm",))
    simple_route("/api/v1/playback/pause", ("pause",))
    simple_route("/api/v1/playback/resume", ("resume",))
    simple_route("/api/v1/playback/stop", ("stop",))
    simple_route("/api/v1/playback/next", ("prepare_next_run",))
    simple_route("/api/v1/playback/reset", ("reset", "reset_to_default"))
    simple_route("/api/v1/playback/disable", ("disable", "emergency_disable"), preempt_motion=True)
    simple_route("/api/v1/manual/stop", ("manual_stop",))
    simple_route("/api/v1/zero/all", ("zero_all_motors",))
    simple_route("/api/v1/zero/read", ("zero_read",))
    simple_route("/api/v1/zero/skip", ("zero_skip",))
    simple_route("/api/v1/zero/abort", ("zero_abort",))

    @app.post("/api/v1/can/connect")
    def connect_can(
        request: Request,
        body: CanConnectRequest | None = None,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        bindings = (
            None
            if body is None or body.bus_bindings is None
            else [item.model_dump() for item in body.bus_bindings]
        )
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: runtime._invoke(
                ("can_connect",),
                bus_bindings=bindings,
            ),
        )

    @app.post("/api/v1/motors/enable")
    def enable_motors(
        body: EnableMotorsRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: runtime._invoke(
                ("enable_motors",),
                physical_estop_confirmed=body.physical_estop_confirmed,
            ),
            motion_command=True,
        )

    @app.post("/api/v1/trajectory/import")
    def import_trajectory(
        body: TrajectoryImportRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: runtime.import_trajectory(body.path),
        )

    @app.post("/api/v1/trajectory/upload")
    async def upload_trajectory(
        request: Request,
        x_filename: str = Header(),
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        owner = owner_id(request, x_rp1_client_id)
        runtime = hub.session(owner)
        try:
            runtime.lease.refresh(owner)
        except LeaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        filename = Path(x_filename).name
        if not filename.lower().endswith(".npz"):
            raise HTTPException(
                status_code=400, detail="trajectory filename must end with .npz"
            )
        body = await request.body()
        if not body or len(body) > 128 * 1024 * 1024:
            raise HTTPException(
                status_code=413, detail="trajectory must be 1 byte to 128 MiB"
            )
        target = runtime.trajectory_root / filename
        temporary = target.with_suffix(target.suffix + ".part")

        def import_in_worker() -> tuple[Any, dict[str, Any]]:
            with runtime._lease_guard:
                runtime.lease.refresh(owner)
                temporary.write_bytes(body)
                temporary.replace(target)
                result = runtime.import_trajectory(str(target))
                runtime.lease.refresh(owner)
                return result, runtime.snapshot()

        try:
            result, snapshot = await asyncio.to_thread(import_in_worker)
        except (ValueError, RuntimeError) as exc:
            target.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "ok": True,
            "result": _json_result(result),
            "snapshot": snapshot,
        }

    @app.post("/api/v1/playback/start")
    def start(
        body: PlaybackStartRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: runtime._invoke(("start",), **body.model_dump()),
            motion_command=True,
        )

    @app.post("/api/v1/manual/move")
    def manual_move(
        body: ManualMoveRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: runtime._invoke(
                ("manual_move",),
                targets_deg=body.targets_deg,
                speed_deg_s=body.speed_deg_s,
            ),
            motion_command=True,
        )

    @app.get("/api/v1/records/current.csv")
    def current_record(
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> FileResponse:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        try:
            path = runtime._invoke(("record_file",))
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if not isinstance(path, Path):
            path = Path(str(path))
        return FileResponse(
            path,
            media_type="text/csv",
            filename=path.name,
        )

    @app.get("/api/v1/platform/bench")
    def read_bench(
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        path = hub.data_root / "bench-code"
        code = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        return {"ok": True, "bench_code": code}

    @app.post("/api/v1/platform/bench")
    def write_bench(
        body: BenchCodeRequest,
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        path = hub.data_root / "bench-code"
        path.parent.mkdir(parents=True, exist_ok=True)
        code = body.bench_code.strip().upper()
        path.write_text(code + "\n", encoding="utf-8")
        return {"ok": True, "bench_code": code}

    @app.post("/api/v1/platform/login")
    def platform_login(
        body: PlatformLoginRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        return {"ok": True, "result": runtime.platform_login(body)}

    @app.post("/api/v1/platform/logout")
    def platform_logout(
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        return {"ok": True, "result": runtime.platform_logout()}

    @app.get("/api/v1/executions")
    def execution_history(
        request: Request,
        limit: int = 100,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        return {
            "items": runtime._invoke(
                ("execution_history",),
                max(1, min(limit, 1000)),
            )
        }

    @app.get("/api/v1/executions/{test_id}/summary")
    def execution_summary(
        test_id: str,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        try:
            return runtime._invoke(("execution_summary",), test_id)
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def execution_file(
        test_id: str,
        request: Request,
        client_id: str | None,
        token: str | None,
        method: str,
        media_type: str,
    ) -> FileResponse:
        require_token(token)
        runtime = hub.session(owner_id(request, client_id))
        try:
            path = runtime._invoke((method,), test_id)
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        resolved = path if isinstance(path, Path) else Path(str(path))
        return FileResponse(
            resolved,
            media_type=media_type,
            filename=resolved.name,
        )

    @app.get("/api/v1/executions/{test_id}/report")
    def execution_report(
        test_id: str,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> FileResponse:
        return execution_file(
            test_id,
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            "execution_report_file",
            "text/markdown",
        )

    @app.get("/api/v1/executions/{test_id}/bundle")
    def execution_bundle(
        test_id: str,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> FileResponse:
        return execution_file(
            test_id,
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            "execution_bundle_file",
            "application/gzip",
        )

    @app.get("/api/v1/executions/{test_id}/sync")
    def execution_sync_status(
        test_id: str,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        return runtime._invoke(("sync_status",), test_id)

    @app.post("/api/v1/executions/rebuild")
    async def rebuild_execution(
        body: ExecutionActionRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        try:
            result = await asyncio.to_thread(
                runtime._invoke,
                ("rebuild_report",),
                body.test_id,
            )
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "result": result}

    @app.post("/api/v1/executions/submit")
    def submit_execution(
        body: ExecutionActionRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        try:
            result = runtime.submit_execution(
                body.test_id,
                reason=body.reason,
                verified_status=body.verified_status,
                operator_summary=body.operator_summary,
                review={
                    "tester": body.tester,
                    "bench": body.bench,
                    "outcome": body.outcome,
                    "notes": body.notes,
                    "analysis": body.analysis,
                    "verified_status": body.verified_status,
                    "station_id": body.station_id,
                    "discrepancy_reason": body.discrepancy_reason,
                    "test_name": body.test_name,
                    "actual_object": body.actual_object,
                } if body.verified_status else None,
            )
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "result": result}

    @app.post("/api/v1/executions/import-history")
    def import_history(body: HistoryImportRequest, request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None)) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        try:
            result = runtime._invoke(("import_history",), body.paths, body.model_dump(exclude={"paths"}))
        except (ValueError, RuntimeError, OSError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "items": result}

    @app.post("/api/v1/executions/retry")
    def retry_execution(
        body: ExecutionActionRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        runtime = hub.session(owner_id(request, x_rp1_client_id))
        try:
            result = runtime.submit_execution(body.test_id, retry=True)
        except (ValueError, RuntimeError, FileNotFoundError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "result": result}

    @app.post("/api/v1/lease/heartbeat")
    def heartbeat(
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        require_token(x_rp1_operator_token)
        owner = owner_id(request, x_rp1_client_id)
        runtime = hub.session(owner)
        try:
            runtime.lease.refresh(owner)
        except LeaseConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"ok": True, "lease": runtime.snapshot()["lease"]}

    @app.post("/api/v1/zero/prepare")
    def zero_prepare(
        body: ZeroPrepareRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: runtime._invoke(("zero_prepare",), body.motor_index),
        )

    @app.post("/api/v1/zero/motor")
    def zero_motor(
        body: ZeroMotorRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: runtime._invoke(
                ("zero_motor",),
                body.motor_index,
            ),
        )

    @app.post("/api/v1/zero/confirm")
    def zero_confirm(
        body: ZeroConfirmRequest,
        request: Request,
        x_rp1_client_id: str | None = Header(default=None),
        x_rp1_operator_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        return mutate(
            request,
            x_rp1_client_id,
            x_rp1_operator_token,
            lambda runtime: runtime._invoke(
                ("zero_confirm",),
                allow_on_fault=body.allow_on_fault,
            ),
        )

    @app.websocket("/ws/status")
    async def status_socket(websocket: WebSocket) -> None:
        expected = os.environ.get("RP1_FACTORY_OPERATOR_TOKEN", "").strip()
        supplied = websocket.query_params.get("token", "")
        if expected and supplied != expected:
            await websocket.close(code=1008, reason="invalid operator token")
            return
        client_id = websocket.query_params.get("client_id", "").strip()
        runtime = hub.session(client_id) if client_id else hub.current()
        await websocket.accept()
        period = 1.0 / max(1.0, runtime.status_rate_hz)
        try:
            while True:
                snapshot = await asyncio.to_thread(runtime.snapshot)
                await websocket.send_json(snapshot)
                await asyncio.sleep(period)
        except WebSocketDisconnect:
            return

    return app
