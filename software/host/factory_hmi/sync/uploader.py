"""Independent resumable uploader for local execution bundles."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib import error, parse, request

from .outbox import Outbox, OutboxItem


class PlatformError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


@dataclass(frozen=True)
class OperatorCredentials:
    username: str
    password: str


def read_secret(path: Path | str) -> str:
    secret_path = Path(path).expanduser().resolve(strict=True)
    mode = stat.S_IMODE(secret_path.stat().st_mode)
    if mode & 0o077:
        raise PermissionError(f"secret file must not be accessible by group/other: {secret_path}")
    value = secret_path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file is empty: {secret_path}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class PlatformClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0) -> None:
        value = base_url.strip().rstrip("/")
        parsed = parse.urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("platform URL must be absolute http(s)")
        self.base_url = value
        self.timeout = float(timeout)
        self.access_token = ""

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        body: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/{path.lstrip('/')}"
        request_headers = {"Accept": "application/json"}
        if self.access_token:
            request_headers["Authorization"] = f"Bearer {self.access_token}"
        if payload is not None:
            body = json.dumps(
                dict(payload),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request_headers.update(dict(headers or {}))
        req = request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read()
        except error.HTTPError as exc:
            raw = exc.read()
            try:
                detail = json.loads(raw.decode("utf-8")).get("detail")
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = raw.decode("utf-8", errors="replace")
            raise PlatformError(
                f"{method} {path} failed: {detail or exc.reason}",
                status=exc.code,
            ) from exc
        except error.URLError as exc:
            raise PlatformError(f"cannot reach platform: {exc.reason}") from exc
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlatformError(f"{method} {path} returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise PlatformError(f"{method} {path} returned a non-object response")
        return value

    def station_login(
        self,
        *,
        station_id: str,
        machine_token: str,
        credentials: OperatorCredentials,
    ) -> dict[str, Any]:
        response = self._call(
            "POST",
            "/api/auth/station-login",
            payload={
                "station_id": station_id,
                "machine_token": machine_token,
                "username": credentials.username,
                "password": credentials.password,
            },
        )
        token = str(response.get("access_token") or "")
        if not token:
            raise PlatformError("station login response did not include access_token")
        self.access_token = token
        return response

    def create_submission(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/api/submissions", payload=payload)

    def create_upload(
        self,
        submission_id: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/submissions/{submission_id}/uploads",
            payload=payload,
        )

    def put_chunk(self, upload_id: str, index: int, body: bytes) -> dict[str, Any]:
        return self._call(
            "PUT",
            f"/api/uploads/{upload_id}/chunks/{index}",
            body=body,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Chunk-SHA256": hashlib.sha256(body).hexdigest(),
            },
        )

    def complete_upload(self, upload_id: str) -> dict[str, Any]:
        return self._call("POST", f"/api/uploads/{upload_id}/complete", payload={})

    def submission(self, submission_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/submissions/{submission_id}")

    def heartbeat(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/api/stations/heartbeat", payload=payload)


class UploaderWorker:
    """Poll an Outbox on a non-control thread and resume chunk uploads."""

    def __init__(
        self,
        outbox: Outbox,
        client: PlatformClient,
        *,
        station_id: str,
        machine_token: str,
        chunk_size: int = 4 * 1024 * 1024,
        poll_interval_s: float = 2.0,
        review_poll_s: float = 30.0,
    ) -> None:
        if not station_id.strip():
            raise ValueError("station_id is required")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self.outbox = outbox
        self.client = client
        self.station_id = station_id.strip()
        self.machine_token = machine_token or "local-dev-machine-token-not-required"
        self.chunk_size = int(chunk_size)
        self.poll_interval_s = float(poll_interval_s)
        self.review_poll_s = float(review_poll_s)
        self._credentials: OperatorCredentials | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._presence: dict[str, Any] = {"state": "idle"}
        self._last_heartbeat_at = 0.0

    def set_credentials(self, username: str, password: str) -> None:
        credentials = OperatorCredentials(username.strip(), password)
        if not credentials.username or not credentials.password:
            raise ValueError("operator username and password are required")
        with self._lock:
            self._credentials = credentials
            self.client.access_token = ""
        self._wake.set()

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        """Validate operator and station credentials before accepting UI login."""
        self.set_credentials(username, password)
        with self._lock:
            credentials = self._credentials
        assert credentials is not None
        try:
            return self.client.station_login(
                station_id=self.station_id,
                machine_token=self.machine_token,
                credentials=credentials,
            )
        except Exception:
            self.clear_credentials()
            raise

    def clear_credentials(self) -> None:
        with self._lock:
            self._credentials = None
            self.client.access_token = ""

    @property
    def identity(self) -> str:
        with self._lock:
            return self._credentials.username if self._credentials else ""

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="rp1-factory-uploader",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def set_presence(self, payload: Mapping[str, Any]) -> None:
        with self._lock:
            self._presence = dict(payload)

    def process_once(self) -> int:
        with self._lock:
            credentials = self._credentials
        if credentials is None:
            return 0
        if not self.client.access_token:
            self.client.station_login(
                station_id=self.station_id,
                machine_token=self.machine_token,
                credentials=credentials,
            )
        if time.time() - self._last_heartbeat_at >= 10.0:
            with self._lock:
                presence = dict(self._presence)
            self.client.heartbeat(presence)
            self._last_heartbeat_at = time.time()
        count = 0
        for item in self.outbox.due(limit=10):
            try:
                self._process(item)
            except PlatformError as exc:
                if exc.status == 401:
                    self.client.access_token = ""
                self.outbox.fail(item.execution_uuid, str(exc))
            except (OSError, ValueError) as exc:
                self.outbox.fail(item.execution_uuid, str(exc))
            count += 1
        return count

    def _process(self, item: OutboxItem) -> None:
        if item.state in {"pending_review", "validating"} and item.submission_id:
            self._sync_remote_state(item)
            return
        if item.state == "sync_failed" and item.submission_id:
            response = self.client.submission(item.submission_id)
            remote = str(response.get("state") or response.get("status") or "")
            if remote in {
                "pending_review",
                "publishing",
                "processing",
                "verified",
                "rejected",
                "failed",
            }:
                self._apply_remote_state(item.execution_uuid, response)
                return
        bundle = Path(item.bundle_path).expanduser().resolve(strict=True)
        manifest = item.manifest
        submission_id = item.submission_id
        if not submission_id:
            submission = self.client.create_submission(
                {
                    "execution_uuid": manifest["execution_uuid"],
                    "test_id": manifest.get("test_id", ""),
                    "campaign_id": manifest.get("campaign_id") or "",
                    "test_case_id": manifest["test_case_id"],
                    "test_case_version": manifest.get("test_case_version", ""),
                    "sample_id": manifest["sample_id"],
                    "bench_id": manifest["bench_id"],
                    "robot_id": manifest.get("robot_id", ""),
                    "station_id": self.station_id,
                    "started_at": manifest["started_at"],
                }
            )
            submission_id = str(submission["id"])
            item = self.outbox.update(
                item.execution_uuid,
                submission_id=submission_id,
                remote_state=str(submission.get("state") or ""),
            )
        upload_id = item.upload_id
        size = bundle.stat().st_size
        total_chunks = max(1, math.ceil(size / self.chunk_size))
        if not upload_id:
            upload = self.client.create_upload(
                submission_id,
                {
                    "file_name": bundle.name,
                    "expected_size": size,
                    "expected_sha256": _sha256(bundle),
                    "chunk_size": self.chunk_size,
                    "total_chunks": total_chunks,
                },
            )
            upload_id = str(upload["id"])
            item = self.outbox.update(
                item.execution_uuid,
                state="uploading",
                upload_id=upload_id,
                next_chunk_index=0,
            )
        with bundle.open("rb") as handle:
            for index in range(item.next_chunk_index, total_chunks):
                handle.seek(index * self.chunk_size)
                chunk = handle.read(self.chunk_size)
                if not chunk:
                    raise ValueError("bundle ended before expected final chunk")
                self.client.put_chunk(upload_id, index, chunk)
                item = self.outbox.update(
                    item.execution_uuid,
                    state="uploading",
                    next_chunk_index=index + 1,
                    last_error="",
                )
        completed = self.client.complete_upload(upload_id)
        self._apply_remote_state(item.execution_uuid, completed)

    def _sync_remote_state(self, item: OutboxItem) -> None:
        response = self.client.submission(item.submission_id)
        self._apply_remote_state(item.execution_uuid, response)

    def _apply_remote_state(
        self,
        execution_uuid: str,
        response: Mapping[str, Any],
    ) -> None:
        remote = str(response.get("state") or response.get("status") or "")
        if remote == "verified":
            local = "approved"
        elif remote == "rejected":
            local = "rejected"
        elif remote == "pending_review":
            local = "pending_review"
        elif remote in {"publishing", "processing"}:
            local = "validating"
        elif remote == "failed":
            self.outbox.fail(
                execution_uuid,
                str(response.get("last_error") or "platform processing failed"),
            )
            self.outbox.update(
                execution_uuid,
                remote_state=remote,
                review_reason=str(response.get("review_reason") or ""),
            )
            return
        else:
            local = "validating"
        self.outbox.update(
            execution_uuid,
            state=local,
            remote_state=remote,
            review_reason=str(response.get("review_reason") or ""),
            next_attempt_at=(
                0
                if local in {"approved", "rejected"}
                else time.time() + self.review_poll_s
            ),
            last_error="",
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.process_once()
            except PlatformError:
                self.client.access_token = ""
            self._wake.wait(self.poll_interval_s)
            self._wake.clear()


def worker_from_environment(data_root: Path | str) -> UploaderWorker:
    root = Path(data_root).expanduser().resolve()
    token_path = os.environ.get("RP1_FACTORY_MACHINE_TOKEN_FILE", "").strip()
    machine_token = ""
    if token_path:
        try:
            machine_token = read_secret(token_path)
        except FileNotFoundError:
            machine_token = ""
    if not machine_token:
        machine_token = os.environ.get("RP1_FACTORY_MACHINE_TOKEN", "").strip()
    worker = UploaderWorker(
        Outbox(root / "sync" / "outbox.sqlite3"),
        PlatformClient(
            os.environ.get("RP1_FACTORY_PLATFORM_URL", "http://127.0.0.1:8080")
        ),
        station_id=os.environ.get("RP1_FACTORY_STATION_ID", "").strip(),
        machine_token=machine_token,
    )
    username = os.environ.get("RP1_FACTORY_PLATFORM_USERNAME", "").strip()
    password_file = os.environ.get("RP1_FACTORY_PLATFORM_PASSWORD_FILE", "").strip()
    password = read_secret(password_file) if password_file else ""
    if username and password:
        worker.set_credentials(username, password)
    return worker


__all__ = [
    "OperatorCredentials",
    "PlatformClient",
    "PlatformError",
    "UploaderWorker",
    "read_secret",
    "worker_from_environment",
]
