"""Small, Qt-independent client for the factory HMI gateway."""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping
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
                rendered = json.dumps(detail, ensure_ascii=False, sort_keys=True)
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

    def post(
        self, path: str, payload: Mapping[str, Any] | None = None
    ) -> JsonValue:
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
            "execution_uuid": execution_uuid,
            "campaign_id": campaign_id,
            "test_case_id": test_case_id,
            "test_case_version": test_case_version,
            "bench_id": bench_id,
            "station_id": station_id,
            "operator_id": operator_id,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
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

    def rebuild_execution(self, test_id: str) -> JsonValue:
        return self.post("/api/v1/executions/rebuild", {"test_id": test_id})

    def submit_execution(self, test_id: str) -> JsonValue:
        return self.post("/api/v1/executions/submit", {"test_id": test_id})

    def retry_execution(self, test_id: str) -> JsonValue:
        return self.post("/api/v1/executions/retry", {"test_id": test_id})

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
