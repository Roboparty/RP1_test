"""Session-oriented facade used by the network control gateway."""

from __future__ import annotations

import os
import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from factory_hmi.models import StationState

from ._legacy import motor_default_positions
from .backend import FakeMotorBackend, MotorsPyBackend
from .can_manager import (
    ALLOWED_ARBITRATION_BITRATES,
    ALLOWED_DATA_BITRATES,
    ARBITRATION_BITRATE,
    DATA_BITRATE,
    CanInterfaceStatus,
    CanManager,
)
from .config import load_station_config
from .controller import FactoryController
from .identity import build_execution_code
from .report import ReportManager, ReportResult
from .zeroing import MotorZeroingSession
from factory_hmi.sync.outbox import Outbox


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class FactoryService:
    """Own at most one configured station and expose JSON-friendly operations."""

    def __init__(
        self,
        *,
        data_root: Path | str | None = None,
        report_root: Path | str | None = None,
        can_manager: CanManager | None = None,
        outbox: Outbox | None = None,
    ) -> None:
        root = data_root or os.environ.get(
            "RP1_FACTORY_DATA_ROOT",
            "~/.local/share/rp1-factory-hmi",
        )
        self.data_root = Path(root).expanduser().resolve()
        self.record_root = self.data_root / "records"
        self.trajectory_root = self.data_root / "trajectories"
        self.record_root.mkdir(parents=True, exist_ok=True)
        self.trajectory_root.mkdir(parents=True, exist_ok=True)
        self.report_manager = ReportManager(report_root or self.data_root)
        self.outbox = outbox or Outbox(self.data_root / "sync" / "outbox.sqlite3")
        self._lock = threading.RLock()
        self._controller: FactoryController | None = None
        self._zeroing: MotorZeroingSession | None = None
        self._backend_name: str | None = None
        self._config_path: Path | None = None
        self._last_stop_reason: str | None = None
        self._can_manager = can_manager or CanManager()
        self._can_interfaces: tuple[str, ...] = ()
        self._can_bindings: tuple[dict[str, Any], ...] = ()
        self._fake_can_connected = False
        self._report_threads: set[threading.Thread] = set()
        self._report_errors: dict[str, str] = {}
        recovery = threading.Thread(
            target=self._recover_existing_records,
            name="factory-report-recovery",
            daemon=True,
        )
        self._report_threads.add(recovery)
        recovery.start()

    @property
    def controller(self) -> FactoryController:
        with self._lock:
            if self._controller is None:
                raise RuntimeError("station is not configured")
            return self._controller

    def configure(
        self,
        *,
        config_path: str,
        limb: str,
        backend: str,
        bus_bindings: Sequence[Mapping[str, Any]] | None = None,
        motor_overrides: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        backend = backend.strip().lower()
        if backend not in {"motors_py", "fake"}:
            raise ValueError("backend must be motors_py or fake")
        path = Path(config_path).expanduser().resolve(strict=True)
        with self._lock:
            if self._controller is not None:
                state = self._controller.state
                if state is not StationState.DISCONNECTED:
                    raise RuntimeError(
                        f"disconnect the current station before configuring; state={state.value}"
                    )
                if self.can_status()["all_up"]:
                    raise RuntimeError(
                        "disconnect CAN interfaces before configuring another station"
                    )
            station_config = load_station_config(
                limb,
                path,
                bus_bindings=bus_bindings,
                motor_overrides=motor_overrides,
            )
            can_interfaces = tuple(
                sorted(
                    {
                        entry.interface
                        for entry in station_config.entries
                        if "can" in entry.interface_type.lower()
                    }
                )
            )
            if not can_interfaces:
                raise ValueError("station configuration has no SocketCAN interfaces")
            if bus_bindings is None:
                normalized_bindings = tuple(
                    {
                        "index": index,
                        "interface": interface,
                        "mode": "canfd",
                        "bitrate": ARBITRATION_BITRATE,
                        "dbitrate": DATA_BITRATE,
                    }
                    for index, interface in enumerate(can_interfaces)
                )
            else:
                normalized_bindings = tuple(dict(item) for item in bus_bindings)
                timing_by_interface: dict[str, tuple[object, object, object]] = {}
                for item in normalized_bindings:
                    interface = str(item.get("interface", ""))
                    timing = (
                        item.get("mode", "canfd"),
                        item.get("bitrate", ARBITRATION_BITRATE),
                        item.get("dbitrate", DATA_BITRATE),
                    )
                    previous = timing_by_interface.setdefault(interface, timing)
                    if previous != timing:
                        raise ValueError(
                            f"reused CAN interface {interface} must use identical timing"
                        )
            if backend == "fake":
                initial = motor_default_positions(
                    station_config.raw,
                    station_config.limb,
                )
                motor_backend = FakeMotorBackend(
                    station_config.entries,
                    initial_positions=initial,
                )
            else:
                motor_backend = MotorsPyBackend(station_config.entries)
            self._controller = FactoryController(
                station_config,
                motor_backend,
                enable_recording=False,
            )
            self._zeroing = None
            self._backend_name = backend
            self._config_path = path
            self._last_stop_reason = None
            self._can_interfaces = can_interfaces
            self._can_bindings = normalized_bindings
            self._fake_can_connected = False
            return self.snapshot()

    def clear_configuration(self) -> dict[str, Any]:
        """Safely stop hardware and remove the active station configuration."""

        with self._lock:
            if self._controller is not None:
                self._controller.disconnect()
            if self._backend_name == "fake":
                self._fake_can_connected = False
            elif self._can_interfaces:
                self._can_manager.disconnect(self._can_interfaces)
            self._controller = None
            self._zeroing = None
            self._backend_name = None
            self._config_path = None
            self._last_stop_reason = None
            self._can_interfaces = ()
            self._can_bindings = ()
            self._fake_can_connected = False
            return self.snapshot()

    def _can_payload(
        self,
        statuses: list[CanInterfaceStatus],
    ) -> dict[str, Any]:
        interfaces = [status.to_dict() for status in statuses]
        expected = {
            str(item["interface"]): item
            for item in self._can_bindings
            if item.get("interface")
        }

        def healthy(status: CanInterfaceStatus) -> bool:
            binding = expected.get(status.interface)
            if binding is None:
                return False
            fd = str(binding.get("mode", "canfd")).lower() == "canfd"
            timing_matches = (
                status.bitrate == int(binding.get("bitrate", ARBITRATION_BITRATE))
                and status.fd is fd
            )
            if fd:
                timing_matches = timing_matches and status.dbitrate == int(
                    binding.get("dbitrate", DATA_BITRATE)
                )
            return (
                status.exists
                and status.up
                and timing_matches
                and status.bus_state not in {"bus-off", "stopped"}
            )

        all_up = bool(statuses) and all(
            healthy(status) for status in statuses
        )
        return {
            "connected": all_up,
            "all_up": all_up,
            "bitrate": (
                self._can_bindings[0].get("bitrate")
                if len(self._can_bindings) == 1
                else None
            ),
            "dbitrate": (
                self._can_bindings[0].get("dbitrate")
                if len(self._can_bindings) == 1
                else None
            ),
            "fd": (
                self._can_bindings[0].get("mode", "canfd") == "canfd"
                if len(self._can_bindings) == 1
                else None
            ),
            "bindings": [dict(item) for item in self._can_bindings],
            "interfaces": interfaces,
        }

    def _fake_can_statuses(self) -> list[CanInterfaceStatus]:
        connected = self._fake_can_connected
        return [
            CanInterfaceStatus(
                interface=str(binding["interface"]),
                exists=True,
                up=connected,
                operstate="up" if connected else "down",
                bitrate=int(binding["bitrate"]) if connected else None,
                dbitrate=(
                    int(binding["dbitrate"])
                    if connected
                    and binding.get("mode", "canfd") == "canfd"
                    and binding.get("dbitrate") is not None
                    else None
                ),
                fd=(
                    binding.get("mode", "canfd") == "canfd"
                    if connected
                    else None
                ),
                bus_state="error-active" if connected else "stopped",
                tx_errors=0,
                rx_errors=0,
                evidence=("fake: virtual SocketCAN state",),
            )
            for binding in self._can_bindings
        ]

    def can_status(self) -> dict[str, Any]:
        with self._lock:
            if not self._can_interfaces:
                return self._can_payload([])
            if self._backend_name == "fake":
                return self._can_payload(self._fake_can_statuses())
            return self._can_payload(
                self._can_manager.status(self._can_interfaces)
            )

    def can_interfaces(self) -> dict[str, Any]:
        """Discover policy-approved CAN links independently of station YAML."""

        allowed_interfaces = tuple(f"can{index}" for index in range(5))
        policy_payload: dict[str, Any] = {
            "allowed_interfaces": list(allowed_interfaces),
            "allowed_bitrates": sorted(ALLOWED_ARBITRATION_BITRATES),
            "allowed_dbitrates": sorted(ALLOWED_DATA_BITRATES),
            "allow_classic_can": True,
            "allow_can_fd": True,
        }
        try:
            from factory_hmi.can_helper import load_policy

            policy = load_policy()
            allowed_interfaces = tuple(sorted(policy.allowed_interfaces))
            policy_payload = {
                "allowed_interfaces": list(allowed_interfaces),
                "allowed_bitrates": sorted(policy.allowed_bitrates),
                "allowed_dbitrates": sorted(policy.allowed_dbitrates),
                "allow_classic_can": policy.allow_classic_can,
                "allow_can_fd": policy.allow_can_fd,
            }
        except Exception:
            # Source-tree and fake-backend development may not have an
            # installed root-owned policy; the helper still enforces it in
            # production before performing any privileged operation.
            pass

        try:
            existing = set(self._can_manager.list_interfaces())
            names = tuple(sorted(existing | set(allowed_interfaces)))
            statuses = self._can_manager.status(names)
        except Exception:
            names = allowed_interfaces
            statuses = [
                CanInterfaceStatus(
                    interface=name,
                    exists=False,
                    up=False,
                    operstate=None,
                    bitrate=None,
                    dbitrate=None,
                    fd=None,
                    bus_state=None,
                    tx_errors=None,
                    rx_errors=None,
                    evidence=("netlink enumeration unavailable",),
                )
                for name in names
            ]
        return {
            "items": [status.to_dict() for status in statuses],
            "policy": policy_payload,
        }

    def can_connect(
        self,
        *,
        bus_bindings: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if not self._can_interfaces:
                raise RuntimeError("configure a station before connecting CAN")
            if bus_bindings is not None:
                requested = tuple(dict(item) for item in bus_bindings)
                if requested != self._can_bindings:
                    raise ValueError(
                        "CAN bindings changed after configuration; apply configuration again"
                    )
            if self._backend_name == "fake":
                self._fake_can_connected = True
            else:
                connected: list[str] = []
                try:
                    for binding in self._can_bindings:
                        interface = str(binding["interface"])
                        if interface in connected:
                            continue
                        self._can_manager.connect(
                            [interface],
                            bitrate=int(binding["bitrate"]),
                            dbitrate=(
                                int(binding["dbitrate"])
                                if binding.get("dbitrate") is not None
                                else None
                            ),
                            fd=binding.get("mode", "canfd") == "canfd",
                        )
                        connected.append(interface)
                except Exception:
                    if connected:
                        self._can_manager.disconnect(connected)
                    raise
                statuses = self._can_manager.status(self._can_interfaces)
                payload = self._can_payload(statuses)
                if not payload["all_up"]:
                    try:
                        self._can_manager.disconnect(self._can_interfaces)
                    finally:
                        raise RuntimeError(
                            "CAN helper completed but interfaces are not healthy"
                        )
            return self.snapshot()

    def can_disconnect(self) -> dict[str, Any]:
        with self._lock:
            if self._controller is not None and self._controller.backend.connected:
                raise RuntimeError("disconnect motors before disconnecting CAN")
            if self._backend_name == "fake":
                self._fake_can_connected = False
            elif self._can_interfaces:
                self._can_manager.disconnect(self._can_interfaces)
            return self.snapshot()

    def can_recover(self) -> dict[str, Any]:
        with self._lock:
            if self._controller is not None and self._controller.backend.connected:
                raise RuntimeError("disconnect motors before recovering CAN")
            if self._backend_name == "fake":
                self._fake_can_connected = True
            else:
                for binding in self._can_bindings:
                    self._can_manager.recover(
                        [str(binding["interface"])],
                        bitrate=int(binding["bitrate"]),
                        dbitrate=(
                            int(binding["dbitrate"])
                            if binding.get("dbitrate") is not None
                            else None
                        ),
                        fd=binding.get("mode", "canfd") == "canfd",
                    )
                statuses = self._can_manager.status(self._can_interfaces)
                if not self._can_payload(statuses)["all_up"]:
                    raise RuntimeError("CAN recovery did not restore healthy interfaces")
            return self.snapshot()

    def connect(self) -> dict[str, Any]:
        """Backward-compatible discovery plus enable operation."""

        if not self.can_status()["all_up"]:
            self.can_connect()
        self.discover_motors()
        return self.enable_motors(physical_estop_confirmed=True)

    def discover_motors(self) -> dict[str, Any]:
        if not self.can_status()["all_up"]:
            raise RuntimeError("connect and validate all CAN interfaces first")
        self.controller.discover_motors()
        return self.snapshot()

    def enable_motors(
        self,
        *,
        physical_estop_confirmed: bool,
    ) -> dict[str, Any]:
        if not physical_estop_confirmed:
            raise RuntimeError(
                "physical emergency-stop and safety circuit confirmation is required"
            )
        controller = self.controller
        was_enabled = controller.backend.motors_enabled
        try:
            controller.enable_motors()
        except Exception:
            # A partially failed all-motor enable leaves the transport open and
            # makes subsequent discover/configure actions report confusing
            # state conflicts. Close the motor transport deterministically;
            # SocketCAN itself stays up so the operator can correct the hardware
            # fault and scan again. A duplicate enable request must not tear
            # down an already healthy, position-holding station.
            if not was_enabled:
                try:
                    controller.disconnect()
                except Exception:
                    pass
            raise
        return self.snapshot()

    def disable_motors(self) -> dict[str, Any]:
        controller = self.controller
        controller.disable_motors()
        return self.snapshot()

    def disconnect(self) -> dict[str, Any]:
        controller = self.controller
        controller.disconnect()
        with self._lock:
            self._zeroing = None
        return self.snapshot()

    def import_trajectory(self, path: str) -> dict[str, Any]:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("trajectory path must be a file")
        safe_name = _SAFE_NAME.sub("_", source.name).strip("._")
        if not safe_name:
            raise ValueError("trajectory filename is invalid")
        destination = (self.trajectory_root / safe_name).resolve()
        if destination.parent != self.trajectory_root.resolve():
            raise ValueError("trajectory destination escapes the data directory")
        if source != destination:
            shutil.copy2(source, destination)
        trajectory = self.controller.load_trajectory(destination)
        return trajectory.to_dict()

    load_trajectory = import_trajectory

    def arm(self) -> dict[str, Any]:
        self.controller.arm()
        return self.snapshot()

    def start(
        self,
        *,
        speed: float = 1.0,
        loop: bool = True,
        cycles: int = 0,
        duration_hours: float | None = None,
        record: bool = True,
        record_rate_hz: float = 20.0,
        test_id: str | None = None,
        robot_id: str | None = None,
        execution_uuid: str | None = None,
        campaign_id: str | None = None,
        test_case_id: str | None = None,
        test_case_version: str | None = None,
        bench_id: str | None = None,
        station_id: str | None = None,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        controller = self.controller
        report_context: dict[str, Any] | None = None
        record_path: Path | None = None
        effective_test_id = test_id
        if record:
            started_at = datetime.now(timezone.utc)
            resolved_test_case = test_case_id or controller.config.limb
            resolved_sample = robot_id or ""
            resolved_bench = bench_id or os.environ.get("RP1_FACTORY_BENCH_ID", "")
            canonical_test_id = build_execution_code(
                resolved_test_case,
                started_at,
                resolved_sample,
                resolved_bench,
            )
            if test_id and test_id != canonical_test_id:
                raise ValueError(
                    "test_id does not match the canonical execution identity"
                )
            safe_test_id = _SAFE_NAME.sub("_", canonical_test_id).strip("._")
            effective_test_id = safe_test_id
            if not safe_test_id:
                raise ValueError("test_id does not contain a usable filename")
            controller.enable_recording = True
            record_path = self.record_root / f"{safe_test_id}.csv"
            controller.record_output = record_path
            trajectory = controller.trajectory
            execution_id = str(execution_uuid or uuid.uuid4())
            uuid.UUID(execution_id)
            now = started_at.isoformat().replace("+00:00", "Z")
            recording_backend = self._backend_name or "unknown"
            source_kind = "real" if recording_backend == "motors_py" else "synthetic"
            report_config = {
                **controller.config.raw,
                "backend": recording_backend,
                "source_kind": source_kind,
            }
            report_context = {
                "execution_uuid": execution_id,
                "test_id": safe_test_id,
                "campaign_id": campaign_id,
                "test_case_id": resolved_test_case,
                "test_case_version": test_case_version or "",
                "sample_id": resolved_sample,
                "robot_id": resolved_sample,
                "bench_id": resolved_bench,
                "station_id": station_id
                or os.environ.get("RP1_FACTORY_STATION_ID", ""),
                "operator_id": operator_id or "",
                "backend": recording_backend,
                "source_kind": source_kind,
                "started_at": now,
                "record_rate_hz": float(record_rate_hz),
                "module_name": controller.config.limb,
                "limb": controller.config.limb,
                "entries": [
                    {
                        "motor_id": int(getattr(entry, "motor_id", index)),
                        "joint_name": str(
                            getattr(entry, "joint_name", f"joint_{index}")
                        ),
                        "module_name": controller.config.limb,
                    }
                    for index, entry in enumerate(controller.config.entries)
                ],
                "software": {
                    "hmi_version": os.environ.get(
                        "RP1_FACTORY_HMI_VERSION", "0.1.0"
                    )
                },
                "config": report_config,
                "acceptance": controller.config.raw.get(
                    "factory_hmi_acceptance", {}
                ),
                "trajectory": trajectory.to_dict() if trajectory else {},
            }
            sidecar = record_path.with_suffix(".context.json")
            sidecar.write_text(
                json.dumps(
                    report_context,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
        else:
            controller.enable_recording = False
            controller.record_output = None
        controller.start(
            loop=loop,
            cycles=cycles,
            duration_s=(
                None
                if duration_hours is None
                else float(duration_hours) * 3600.0
            ),
            speed=speed,
            record_rate_hz=record_rate_hz,
            test_id=effective_test_id,
            robot_id=robot_id,
        )
        if report_context is not None and record_path is not None:
            thread = threading.Thread(
                target=self._finalize_after_run,
                args=(controller, record_path, report_context),
                name=f"factory-report-{report_context['test_id']}",
                daemon=True,
            )
            with self._lock:
                self._report_threads.add(thread)
            thread.start()
        return self.snapshot()

    def pause(self) -> dict[str, Any]:
        self.controller.pause()
        return self.snapshot()

    def resume(self) -> dict[str, Any]:
        self.controller.resume()
        return self.snapshot()

    def stop(self, *, reason: str | None = None) -> dict[str, Any]:
        self._last_stop_reason = reason
        self.controller.stop()
        return self.snapshot()

    def prepare_next_run(self) -> dict[str, Any]:
        self.controller.prepare_next_run()
        return self.snapshot()

    def manual_move(
        self,
        *,
        targets_deg: Sequence[float],
        speed_deg_s: float,
    ) -> dict[str, Any]:
        controller = self.controller
        if len(targets_deg) != len(controller.config.entries):
            raise ValueError(
                f"manual target requires {len(controller.config.entries)} joints, "
                f"got {len(targets_deg)}"
            )
        controller.manual_move(
            np.deg2rad(np.asarray(targets_deg, dtype=np.float64)),
            speed_rad_s=float(np.deg2rad(speed_deg_s)),
        )
        return self.snapshot()

    def manual_stop(self) -> dict[str, Any]:
        self.controller.manual_stop()
        return self.snapshot()

    def record_file(self) -> Path:
        output = self.controller.record_output
        if output is None:
            raise RuntimeError("no recorded CSV is available")
        resolved = Path(output).expanduser().resolve()
        root = self.record_root.resolve()
        if resolved.parent != root:
            raise RuntimeError("recorded CSV is outside the records directory")
        if not resolved.is_file():
            raise RuntimeError("recorded CSV has not been finalized")
        return resolved

    def execution_history(self, limit: int = 100) -> list[dict[str, Any]]:
        items = self.report_manager.history(limit=limit)
        by_test_id = {item.test_id: item for item in self.outbox.list(limit=limit)}
        for item in items:
            queued = by_test_id.get(str(item["test_id"]))
            item.update(
                {
                    "sync_state": queued.state if queued else "local_completed",
                    "sync_error": queued.last_error if queued else "",
                    "review_reason": queued.review_reason if queued else "",
                    "submission_id": queued.submission_id if queued else "",
                }
            )
        return items

    def execution_summary(self, test_id: str) -> dict[str, Any]:
        return self.report_manager.summary(test_id)

    def execution_report_file(self, test_id: str) -> Path:
        return self.report_manager.report_path(test_id)

    def execution_bundle_file(self, test_id: str) -> Path:
        return self.report_manager.bundle_path(test_id)

    def rebuild_report(self, test_id: str) -> dict[str, Any]:
        result = self.report_manager.rebuild(test_id)
        return self._report_result(result)

    def submit_execution(self, test_id: str) -> dict[str, Any]:
        item = self.outbox.enqueue(
            self.report_manager.manifest(test_id),
            self.report_manager.bundle_path(test_id),
        )
        return item.to_dict()

    def retry_submission(self, test_id: str) -> dict[str, Any]:
        item = self.outbox.get_by_test_id(test_id)
        if item is None:
            return self.submit_execution(test_id)
        return self.outbox.retry(item.execution_uuid).to_dict()

    def sync_status(self, test_id: str) -> dict[str, Any]:
        item = self.outbox.get_by_test_id(test_id)
        return (
            item.to_dict()
            if item is not None
            else {"test_id": test_id, "state": "local_completed"}
        )

    def recover_reports(self) -> list[dict[str, Any]]:
        return [
            self._report_result(result)
            for result in self.report_manager.recover_orphans(self.record_root)
        ]

    def _recover_existing_records(self) -> None:
        current = threading.current_thread()
        try:
            self.recover_reports()
        except Exception as exc:
            with self._lock:
                self._report_errors["startup-recovery"] = str(exc)
        finally:
            with self._lock:
                self._report_threads.discard(current)

    def _finalize_after_run(
        self,
        controller: FactoryController,
        record_path: Path,
        context: dict[str, Any],
    ) -> None:
        current = threading.current_thread()
        test_id = str(context["test_id"])
        try:
            controller.wait()
            snapshot = controller.snapshot()
            playback = snapshot.get("playback")
            progress = (
                playback.get("progress", {})
                if isinstance(playback, Mapping)
                else {}
            )
            fault = snapshot.get("fault")
            context.update(
                {
                    "ended_at": datetime.now(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "active_seconds": float(progress.get("active_seconds") or 0),
                    "completed_cycles": int(
                        progress.get("completed_cycles") or 0
                    ),
                    "outcome": str(progress.get("outcome") or "interrupted"),
                    "manual_stop": progress.get("outcome") == "stopped",
                    "safety_failure": bool(fault),
                    "stop_reason": (
                        str(fault.get("message") or "")
                        if isinstance(fault, Mapping)
                        else self._last_stop_reason or ""
                    ),
                }
            )
            self.report_manager.finalize(record_path, context)
            record_path.with_suffix(".context.json").unlink(missing_ok=True)
            with self._lock:
                self._report_errors.pop(test_id, None)
        except Exception as exc:
            with self._lock:
                self._report_errors[test_id] = str(exc)
        finally:
            with self._lock:
                self._report_threads.discard(current)

    @staticmethod
    def _report_result(result: ReportResult) -> dict[str, Any]:
        return {
            "test_id": result.manifest["test_id"],
            "execution_uuid": result.manifest["execution_uuid"],
            "bundle_path": str(result.bundle_path),
            "summary_path": str(result.summary_path),
            "report_path": str(result.report_path),
            "verdict": result.summary["verdict"]["status"],
        }

    def reset(self) -> dict[str, Any]:
        controller = self.controller
        controller.reset(to_default=True)
        return self.snapshot()

    reset_to_default = reset

    def disable(self) -> dict[str, Any]:
        controller = self.controller
        if controller.state in {StationState.RUNNING, StationState.PAUSED}:
            controller.stop()
        controller.disconnect()
        return self.snapshot()

    emergency_disable = disable

    def zero_motor(self, motor_index: int) -> dict[str, Any]:
        result = self.controller.zero_motors([int(motor_index)])
        return {
            **result,
            "snapshot": self.snapshot(),
        }

    def zero_all_motors(self) -> dict[str, Any]:
        controller = self.controller
        result = controller.zero_motors(
            list(range(len(controller.config.entries)))
        )
        return {
            **result,
            "snapshot": self.snapshot(),
        }

    def zero_prepare(self, motor_index: int) -> dict[str, Any]:
        controller = self.controller
        with self._lock:
            if self._zeroing is not None and self._zeroing.state.value == "reading":
                if self._zeroing.current_index == int(motor_index):
                    return self._zeroing.snapshot()
                raise RuntimeError(
                    "finish or abort the current motor zeroing before selecting another"
                )
            # Each row action is an independent single-motor operation. A
            # confirmed motor must not force the operator through the remaining
            # motor list or prevent selecting the same motor again later.
            self._zeroing = controller.create_zeroing_session()
            return self._zeroing.prepare(motor_index)

    def zero_read(self) -> dict[str, Any]:
        return self._require_zeroing().read().to_dict()

    def zero_confirm(self, *, allow_on_fault: bool = False) -> dict[str, Any]:
        if allow_on_fault:
            raise ValueError("factory gateway does not permit fault-state zeroing")
        result = self._require_zeroing().confirm(force_on_error=False)
        return {"zeroed": bool(result), "zeroing": self._require_zeroing().snapshot()}

    def zero_skip(self) -> dict[str, Any]:
        skipped = self._require_zeroing().skip()
        return {"skipped_index": skipped, "zeroing": self._require_zeroing().snapshot()}

    def zero_abort(self) -> dict[str, Any]:
        zeroing = self._require_zeroing()
        zeroing.abort()
        return zeroing.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._controller is None:
                return {
                    "state": "disconnected",
                    "connected": False,
                    "configured": False,
                    "station": {
                        "connection_phase": "can_disconnected",
                        "can_connected": False,
                        "motors_discovered": False,
                        "motors_enabled": False,
                    },
                    "motors": [],
                    "configured_motors": [],
                    "trajectory": None,
                    "progress": None,
                    "can": self._can_payload([]),
                    "fault": None,
                    "diagnostics": [],
                }
            snapshot = self._controller.snapshot()
            configured_motors = [
                {
                    "index": int(entry.index),
                    "motor_id": int(entry.motor_id),
                    "joint_name": entry.joint_name,
                    "bus": entry.interface,
                    "motor_type": entry.motor_type,
                    "motor_model": int(entry.motor_model),
                }
                for entry in self._controller.config.entries
            ]
            station = dict(snapshot.get("station") or {})
            state = str(station.get("state", self._controller.state.value))
            fault = snapshot.get("fault")
            diagnoses = fault.get("diagnoses", []) if isinstance(fault, dict) else []
            can_snapshot = self.can_status()
            motors_discovered = bool(station.get("backend_connected", False))
            motors_enabled = bool(station.get("motors_enabled", False))
            if motors_enabled:
                connection_phase = "motors_enabled"
            elif motors_discovered:
                connection_phase = "motors_discovered"
            elif can_snapshot["all_up"]:
                connection_phase = "can_connected"
            else:
                connection_phase = "can_disconnected"
            station.update(
                {
                    "connection_phase": connection_phase,
                    "can_connected": bool(can_snapshot["all_up"]),
                    "motors_discovered": motors_discovered,
                    "motors_enabled": motors_enabled,
                }
            )
            snapshot.update(
                {
                    "state": state,
                    "connected": motors_discovered,
                    "configured": True,
                    "backend": self._backend_name,
                    "config_path": str(self._config_path) if self._config_path else None,
                    "configured_motors": configured_motors,
                    "station": station,
                    "can": can_snapshot,
                    "diagnostics": diagnoses,
                    "last_stop_reason": self._last_stop_reason,
                    "report_errors": dict(self._report_errors),
                    "zeroing": (
                        self._zeroing.snapshot()
                        if self._zeroing is not None
                        else snapshot.get("zeroing")
                    ),
                }
            )
            return snapshot

    def _require_zeroing(self) -> MotorZeroingSession:
        with self._lock:
            if self._zeroing is None:
                raise RuntimeError("zeroing session has not been prepared")
            return self._zeroing


__all__ = ["FactoryService"]
