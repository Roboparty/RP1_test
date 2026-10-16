"""Small, Qt-independent client for the factory HMI gateway."""

from __future__ import annotations

import json
import threading
from copy import deepcopy
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib import error, parse, request

JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None


class GatewayError(RuntimeError):
    """Base error raised when the gateway cannot satisfy a request."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any = None,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        self.status_code = status_code
        # ``status`` is kept as a convenient alias for UI and callers.
        self.status = status_code
        self.detail = detail
        self.service_detail = detail
        self.method = method
        self.url = url

        parts = [message]
        if status_code is not None:
            parts.append(f"HTTP {status_code}")
        if detail not in (None, ""):
            if isinstance(detail, (dict, list)):
                rendered = str(detail["message"]) if isinstance(detail, Mapping) and detail.get("message") else json.dumps(detail, ensure_ascii=False, sort_keys=True)
            else:
                rendered = str(detail)
            parts.append(f"gateway detail: {rendered}")
        super().__init__("; ".join(parts))


class GatewayHTTPError(GatewayError):
    """An HTTP response with a non-success status."""


class GatewayClient:
    """Synchronous REST client.

    The desktop application runs these calls in ``QThreadPool`` workers, while
    tests and non-Qt tools can use the same class directly.
    """

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        client_id: str | None = None,
        operator_token: str | None = None,
    ) -> None:
        base_url = base_url.strip().rstrip("/")
        parsed = parse.urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("gateway URL must be an absolute http(s) URL")
        self.base_url = base_url
        self.timeout = timeout
        self.client_id = (client_id or f"hmi-{uuid.uuid4()}").strip()
        self.operator_token = (operator_token or "").strip()
        self._plc_lock = threading.RLock()
        self._plc_snapshot: dict[str, Any] = {}
        self._plc_retired_epochs: set[str] = set()

    @property
    def status_ws_url(self) -> str:
        """Return the WebSocket endpoint corresponding to this gateway."""

        parsed = parse.urlsplit(self.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        parameters = {"client_id": self.client_id}
        if self.operator_token:
            parameters["token"] = self.operator_token
        query = parse.urlencode(parameters)
        return parse.urlunsplit((scheme, parsed.netloc, "/ws/status", query, ""))

    @property
    def ws_url(self) -> str:
        """Backward-friendly alias for the status WebSocket URL."""

        return self.status_ws_url

    def build_ws_url(self) -> str:
        return self.status_ws_url

    def _url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    @staticmethod
    def _decode_response(raw: bytes, *, method: str, url: str) -> JsonValue:
        if not raw or not raw.strip():
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GatewayError(
                "gateway returned invalid JSON",
                detail=str(exc),
                method=method,
                url=url,
            ) from exc

    @staticmethod
    def _decode_error_detail(raw: bytes) -> Any:
        if not raw:
            return None
        try:
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return raw.decode("utf-8", errors="replace").strip()
        if isinstance(payload, Mapping):
            for key in ("detail", "message", "error"):
                if key in payload:
                    return payload[key]
        return payload

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
    ) -> JsonValue:
        url = self._url(path)
        headers = {
            "Accept": "application/json",
            "X-RP1-Client-ID": self.client_id,
        }
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        data = None
        if method != "GET":
            data = json.dumps(
                dict(payload or {}), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return self._decode_response(
                    response.read(),
                    method=method,
                    url=url,
                )
        except error.HTTPError as exc:
            try:
                raw = exc.read()
            except (AttributeError, OSError):
                raw = b""
            detail = self._decode_error_detail(raw)
            raise GatewayHTTPError(
                "gateway request failed",
                status_code=exc.code,
                detail=detail,
                method=method,
                url=url,
            ) from exc
        except error.URLError as exc:
            detail = getattr(exc, "reason", exc)
            raise GatewayError(
                "cannot reach gateway",
                detail=detail,
                method=method,
                url=url,
            ) from exc

    def get(self, path: str) -> JsonValue:
        return self._request("GET", path)

    def post(self, path: str, payload: Mapping[str, Any] | None = None) -> JsonValue:
        return self._request("POST", path, payload)

    def download(self, path: str, destination: Path | str) -> Path:
        url = self._url(path)
        headers = {"X-RP1-Client-ID": self.client_id}
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        req = request.Request(url, headers=headers, method="GET")
        target = Path(destination).expanduser()
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                payload = response.read()
        except error.HTTPError as exc:
            detail = self._decode_error_detail(exc.read())
            raise GatewayHTTPError(
                "gateway download failed",
                status_code=exc.code,
                detail=detail,
                method="GET",
                url=url,
            ) from exc
        except error.URLError as exc:
            raise GatewayError(
                "cannot reach gateway",
                detail=getattr(exc, "reason", exc),
                method="GET",
                url=url,
            ) from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return target

    def health(self) -> JsonValue:
        return self.get("/api/v1/health")

    def snapshot(self) -> JsonValue:
        return self.get("/api/v1/snapshot")

    def configs(self) -> JsonValue:
        return self.get("/api/v1/configs")

    def configure(
        self,
        config_path: str,
        limb: str,
        backend: str,
        *,
        bus_bindings: list[Mapping[str, Any]] | None = None,
        motor_overrides: list[Mapping[str, Any]] | None = None,
    ) -> JsonValue:
        payload: dict[str, Any] = {
            "config_path": config_path,
            "limb": limb,
            "backend": backend,
        }
        if bus_bindings is not None:
            payload["bus_bindings"] = bus_bindings
        if motor_overrides is not None:
            payload["motor_overrides"] = motor_overrides
        return self.post(
            "/api/v1/configure",
            payload,
        )

    def clear_configuration(self) -> JsonValue:
        return self.post("/api/v1/configuration/clear")

    def config_preview(self, config_path: str, limb: str) -> JsonValue:
        return self.post(
            "/api/v1/config/preview",
            {"config_path": config_path, "limb": limb},
        )

    def config_upload(self, path: str | Path, limb: str) -> JsonValue:
        source = Path(path)
        body = source.read_bytes()
        url = self._url("/api/v1/config/upload")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/x-yaml",
            "X-Filename": source.name,
            "X-RP1-Limb": limb,
            "X-RP1-Client-ID": self.client_id,
        }
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return self._decode_response(response.read(), method="POST", url=url)
        except error.HTTPError as exc:
            raise GatewayHTTPError(
                "gateway configuration upload failed",
                status_code=exc.code,
                detail=self._decode_error_detail(exc.read()),
                method="POST",
                url=url,
            ) from exc
        except error.URLError as exc:
            raise GatewayError(
                "cannot reach gateway",
                detail=getattr(exc, "reason", exc),
                method="POST",
                url=url,
            ) from exc

    def connect(self) -> JsonValue:
        return self.post("/api/v1/connect")

    def can_status(self) -> JsonValue:
        return self.get("/api/v1/can/status")

    def can_interfaces(self) -> JsonValue:
        return self.get("/api/v1/can/interfaces")

    def can_connect(
        self,
        bus_bindings: list[Mapping[str, Any]] | None = None,
    ) -> JsonValue:
        payload = None if bus_bindings is None else {"bus_bindings": bus_bindings}
        return self.post("/api/v1/can/connect", payload)

    def can_disconnect(self) -> JsonValue:
        return self.post("/api/v1/can/disconnect")

    def can_recover(self) -> JsonValue:
        return self.post("/api/v1/can/recover")

    def discover_motors(self) -> JsonValue:
        return self.post("/api/v1/motors/discover")

    def enable_motors(self, *, physical_estop_confirmed: bool) -> JsonValue:
        return self.post(
            "/api/v1/motors/enable",
            {"physical_estop_confirmed": physical_estop_confirmed},
        )

    def disable_motors(self) -> JsonValue:
        return self.post("/api/v1/motors/disable")

    def disconnect(self) -> JsonValue:
        return self.post("/api/v1/disconnect")

    def arm(self) -> JsonValue:
        return self.post("/api/v1/arm")

    def trajectory_import(self, path: str) -> JsonValue:
        return self.post("/api/v1/trajectory/import", {"path": path})

    def import_trajectory(self, path: str) -> JsonValue:
        return self.trajectory_import(path)

    def trajectory_upload(self, path: str | Path) -> JsonValue:
        source = Path(path)
        body = source.read_bytes()
        url = self._url("/api/v1/trajectory/upload")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/octet-stream",
            "X-Filename": source.name,
            "X-RP1-Client-ID": self.client_id,
        }
        if self.operator_token:
            headers["X-RP1-Operator-Token"] = self.operator_token
        req = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                return self._decode_response(response.read(), method="POST", url=url)
        except error.HTTPError as exc:
            detail = self._decode_error_detail(exc.read())
            raise GatewayHTTPError(
                "gateway trajectory upload failed",
                status_code=exc.code,
                detail=detail,
                method="POST",
                url=url,
            ) from exc
        except error.URLError as exc:
            raise GatewayError(
                "cannot reach gateway",
                detail=getattr(exc, "reason", exc),
                method="POST",
                url=url,
            ) from exc

    def playback_start(
        self,
        *,
        speed: float,
        loop: bool,
        cycles: int,
        duration_hours: float | None,
        record: bool,
        recording_scope: str | None = None,
        record_rate_hz: float = 20.0,
        test_id: str | None = None,
        robot_id: str | None = None,
        execution_uuid: str | None = None,
        campaign_id: str | None = None,
        cycle_id: str | None = None,
        segment_id: str | None = None,
        asset_id: str | None = None,
        configuration_id: str | None = None,
        test_case_version_id: str | None = None,
        test_case_id: str | None = None,
        test_case_version: str | None = None,
        test_name: str | None = None,
        bench_id: str | None = None,
        station_id: str | None = None,
        operator_id: str | None = None,
        stage_code: str | None = None,
        stage_name: str | None = None,
        subject_map: Mapping[str, str] | None = None,
    ) -> JsonValue:
        payload: dict[str, Any] = {
            "speed": speed,
            "loop": loop,
            "cycles": cycles,
            "duration_hours": duration_hours,
            "record": record,
            "record_rate_hz": record_rate_hz,
            "test_id": test_id,
            "robot_id": robot_id,
        }
        optional = {
            "recording_scope": recording_scope,
            "execution_uuid": execution_uuid,
            "campaign_id": campaign_id,
            "cycle_id": cycle_id,
            "segment_id": segment_id,
            "asset_id": asset_id,
            "configuration_id": configuration_id,
            "test_case_version_id": test_case_version_id,
            "test_case_id": test_case_id,
            "test_case_version": test_case_version,
            "test_name": test_name,
            "bench_id": bench_id,
            "station_id": station_id,
            "operator_id": operator_id,
            "stage_code": stage_code,
            "stage_name": stage_name,
            "subject_map": dict(subject_map) if subject_map else None,
        }
        payload.update(
            {key: value for key, value in optional.items() if value is not None}
        )
        return self.post(
            "/api/v1/playback/start",
            payload,
        )

    def playback_pause(self) -> JsonValue:
        return self.post("/api/v1/playback/pause")

    def playback_resume(self) -> JsonValue:
        return self.post("/api/v1/playback/resume")

    def playback_next(self) -> JsonValue:
        return self.post("/api/v1/playback/next")

    def playback_stop(self) -> JsonValue:
        return self.post("/api/v1/playback/stop")

    def playback_reset(self) -> JsonValue:
        return self.post("/api/v1/playback/reset")

    def reset_motor_fault(self) -> JsonValue:
        return self.post("/api/v1/motors/reset-fault")

    def playback_disable(self) -> JsonValue:
        return self.post("/api/v1/playback/disable")

    def lease_heartbeat(self) -> JsonValue:
        return self.post("/api/v1/lease/heartbeat")

    def manual_move(
        self,
        targets_deg: list[float],
        speed_deg_s: float,
    ) -> JsonValue:
        return self.post(
            "/api/v1/manual/move",
            {
                "targets_deg": targets_deg,
                "speed_deg_s": speed_deg_s,
            },
        )

    def manual_stop(self) -> JsonValue:
        return self.post("/api/v1/manual/stop")

    def download_current_record(self, destination: Path | str) -> Path:
        return self.download("/api/v1/records/current.csv", destination)

    def platform_login(
        self,
        username: str,
        password: str,
        *,
        station_id: str | None = None,
    ) -> JsonValue:
        return self.post(
            "/api/v1/platform/login",
            {
                "username": username,
                "password": password,
                "station_id": station_id,
            },
        )

    def platform_logout(self) -> JsonValue:
        return self.post("/api/v1/platform/logout")

    def platform_bench(self) -> JsonValue:
        return self.get("/api/v1/platform/bench")

    def set_platform_bench(self, bench_code: str) -> JsonValue:
        return self.post("/api/v1/platform/bench", {"bench_code": bench_code})

    def submit_execution(
        self,
        test_id: str,
        reason: str = "",
        *,
        verified_status: str = "",
        operator_summary: str = "",
        review: Mapping[str, Any] | None = None,
    ) -> JsonValue:
        payload = {
            "test_id": test_id,
            "reason": reason,
            "verified_status": verified_status,
            "operator_summary": operator_summary,
            "tester": "",
            "bench": "",
            "outcome": "",
            "notes": "",
            "analysis": "",
        }
        if review:
            payload["operator_summary"] = str(review.get("operator_summary") or operator_summary)
            payload["verified_status"] = str(review.get("verified_status") or verified_status)
            for name in ("tester", "bench", "outcome", "notes", "analysis", "station_id", "discrepancy_reason", "test_name", "actual_object"):
                payload[name] = str(review.get(name) or "")
        return self.post("/api/v1/executions/submit", payload)

    def import_execution_history(self, paths: list[str], metadata: Mapping[str, str]) -> JsonValue:
        return self.post("/api/v1/executions/import-history", {"paths": paths, **dict(metadata)})

    def execution_history(self, *, limit: int = 100) -> JsonValue:
        return self.get(f"/api/v1/executions?limit={int(limit)}")

    def execution_summary(self, test_id: str) -> JsonValue:
        return self.get(f"/api/v1/executions/{parse.quote(test_id, safe='')}/summary")

    def download_execution_report(
        self,
        test_id: str,
        destination: Path | str,
    ) -> Path:
        return self.download(
            f"/api/v1/executions/{parse.quote(test_id, safe='')}/report",
            destination,
        )

    def download_execution_bundle(
        self,
        test_id: str,
        destination: Path | str,
    ) -> Path:
        return self.download(
            f"/api/v1/executions/{parse.quote(test_id, safe='')}/bundle",
            destination,
        )

    def execution_sync_status(self, test_id: str) -> JsonValue:
        return self.get(f"/api/v1/executions/{parse.quote(test_id, safe='')}/sync")

    def platform_options(self) -> JsonValue:
        return self.get("/api/v1/platform/options")

    def rebuild_execution(self, test_id: str) -> JsonValue:
        return self.post("/api/v1/executions/rebuild", {"test_id": test_id})

    def retry_execution(self, test_id: str) -> JsonValue:
        return self.post("/api/v1/executions/retry", {"test_id": test_id})

    def _remember_plc_snapshot(self, payload: JsonValue) -> None:
        value = payload
        if isinstance(value, Mapping) and isinstance(value.get("result"), Mapping):
            value = value["result"]
        if not isinstance(value, Mapping) or not isinstance(value.get("coordination"), Mapping):
            return
        with self._plc_lock:
            old = self._plc_snapshot.get("coordination", {})
            new = value["coordination"]
            epoch = str(new.get("epoch", ""))
            if epoch in self._plc_retired_epochs:
                return
            if old.get("epoch") == epoch:
                if new.get("snapshot_sequence", 0) < old.get("snapshot_sequence", 0):
                    return
            elif old.get("epoch"):
                self._plc_retired_epochs.add(old["epoch"])
            self._plc_snapshot = deepcopy(dict(value))

    def plc_snapshot(self) -> JsonValue:
        result = self.get("/api/v1/plc/snapshot")
        self._remember_plc_snapshot(result)
        return result

    def _plc_post(self, operation: str, payload: Mapping[str, Any],
                  context: Mapping[str, Any] | None = None) -> JsonValue:
        if context is None:
            with self._plc_lock:
                context = dict(self._plc_snapshot.get("coordination", {}))
            if not context and operation != "/api/v1/plc/all-stop":
                self.plc_snapshot()
                with self._plc_lock:
                    context = dict(self._plc_snapshot.get("coordination", {}))
        body = dict(payload)
        body.update(
            request_id=context.get("request_id") or str(uuid.uuid4()),
            expected_epoch=context.get("epoch"),
            expected_revision=context.get("revision"),
        )
        try:
            result = self.post(operation, body)
        except GatewayError as exc:
            if isinstance(exc.detail, Mapping):
                self._remember_plc_snapshot(exc.detail.get("snapshot"))
            # Never automatically refresh and re-send a conflicting command.
            raise
        self._remember_plc_snapshot(result)
        return result

    def plc_connect(self, user: str, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/connect", {"user": user}, context)

    def plc_disconnect(self, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/disconnect", {}, context)

    def plc_start(self, channels: list[int], user: str, voltage: float, current: float,
                  *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/start", {
            "channels": list(channels), "voltage": float(voltage),
            "current": float(current), "user": user,
        }, context)

    def plc_stop(self, user: str, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/stop", {"user": user}, context)

    def plc_all_stop(self, user: str, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/all-stop", {"user": user}, context)

    def plc_reset_fault(self, user: str, *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/reset-fault", {"user": user}, context)

    def plc_set_channel(self, channel: int, enabled: bool, user: str,
                        *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post(f"/api/v1/plc/channels/{int(channel)}", {"enabled": bool(enabled), "user": user}, context)

    def plc_set_ps1_setpoints(self, voltage: float, current: float, user: str,
                              *, context: Mapping[str, Any] | None = None) -> JsonValue:
        return self._plc_post("/api/v1/plc/setpoints", {"voltage": float(voltage), "current": float(current), "user": user}, context)

    def zero_prepare(self, motor_index: int) -> JsonValue:
        return self.post("/api/v1/zero/prepare", {"motor_index": motor_index})

    def zero_motor(self, motor_index: int) -> JsonValue:
        return self.post("/api/v1/zero/motor", {"motor_index": motor_index})

    def zero_all_motors(self) -> JsonValue:
        return self.post("/api/v1/zero/all")

    def zero_read(self) -> JsonValue:
        return self.post("/api/v1/zero/read")

    def zero_confirm(self, *, allow_on_fault: bool = False) -> JsonValue:
        return self.post(
            "/api/v1/zero/confirm",
            {"allow_on_fault": allow_on_fault},
        )

    def zero_skip(self) -> JsonValue:
        return self.post("/api/v1/zero/skip")

    def zero_abort(self) -> JsonValue:
        return self.post("/api/v1/zero/abort")

    # Concise aliases are convenient for wiring command buttons.
    def pause(self) -> JsonValue:
        return self.playback_pause()

    def resume(self) -> JsonValue:
        return self.playback_resume()

    def stop(self) -> JsonValue:
        return self.playback_stop()

    def reset(self) -> JsonValue:
        return self.playback_reset()

    def disable(self) -> JsonValue:
        return self.playback_disable()
