"""Authenticated reliability_v1 summary and verified CSV archive transport."""

from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import tarfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib import error, request

import yaml

from factory_hmi.core.identity import build_execution_code
from .outbox import Outbox, OutboxItem
from .uploader import PlatformError, read_secret


DEFAULT_PROFILE_PATH = (
    Path(__file__).resolve().parents[1] / "config" / "reliability_platform.yaml"
)
_PROFILE_REQUIRED = (
    "campaign_id",
    "cycle_id",
    "segment_id",
    "asset_id",
    "configuration_id",
    "test_case_version_id",
    "station_id",
)


def load_test_profiles(path: Path | str | None = None) -> list[dict[str, Any]]:
    profile_path = Path(path or DEFAULT_PROFILE_PATH).expanduser().resolve()
    raw = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
    profiles = raw.get("profiles") if isinstance(raw, Mapping) else None
    if profiles is None:
        return []
    if not isinstance(profiles, list):
        raise ValueError("reliability platform profiles must be a list")
    result: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw_profile in profiles:
        if not isinstance(raw_profile, Mapping):
            raise ValueError("each reliability platform profile must be a mapping")
        profile = dict(raw_profile)
        profile["test_case_id"] = str(profile.get("test_case_code") or profile.get("case_code") or profile.get("test_case_id") or "").strip().upper()
        profile["sample_id"] = str(profile.get("asset_code") or profile.get("sample_id") or "").strip().upper()
        profile["bench_id"] = str(profile.get("station_code") or profile.get("bench_id") or "").strip().upper()
        key = str(profile.get("key") or "").strip()
        label = str(profile.get("label") or "").strip()
        if not key or not label:
            raise ValueError("each platform profile requires key and label")
        if key in keys:
            raise ValueError(f"duplicate platform profile key: {key}")
        keys.add(key)
        missing = [name for name in _PROFILE_REQUIRED if not str(profile.get(name) or "").strip()]
        missing.extend(name for name in ("test_case_id", "sample_id", "bench_id") if not profile[name])
        for aliases in (("test_case_code", "case_code", "test_case_id"), ("asset_code", "sample_id"), ("station_code", "bench_id")):
            values = {str(raw_profile[name]).strip().upper() for name in aliases if raw_profile.get(name)}
            if len(values) > 1:
                missing.append("/".join(aliases) + " 编号不一致")
        if not missing:
            try:
                build_execution_code(profile["test_case_id"], datetime.now(timezone.utc), profile["sample_id"], profile["bench_id"])
            except ValueError as exc:
                missing.append(str(exc))
        profile["ready"] = not missing
        profile["missing"] = missing
        subject_map = profile.get("subject_map") or {}
        if not isinstance(subject_map, Mapping):
            raise ValueError(f"profile {key} subject_map must be a mapping")
        profile["subject_map"] = {
            str(joint): str(subject)
            for joint, subject in subject_map.items()
            if str(joint).strip() and str(subject).strip()
        }
        result.append(profile)
    return result


def profiles_from_setup(
    campaigns: list[Any],
    contexts_by_campaign: Mapping[str, list[Any]],
    stations: list[Any],
) -> list[dict[str, Any]]:
    """Turn platform campaign contexts into selectable HMI profiles."""
    station_by_id = {
        str(station.get("id")): station
        for station in stations
        if isinstance(station, Mapping) and station.get("id")
    }
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for campaign in campaigns:
        if not isinstance(campaign, Mapping):
            continue
        if str(campaign.get("status") or "") not in {"PLANNED", "ACTIVE"}:
            continue
        campaign_id = str(campaign.get("id") or "")
        raw_stages = campaign.get("stages")
        stages = [
            {
                "sequence_no": int(stage.get("sequence_no") or 1),
                "stage_name": str(stage.get("stage_name") or "").strip(),
            }
            for stage in raw_stages
            if isinstance(stage, Mapping) and str(stage.get("stage_name") or "").strip()
        ] if isinstance(raw_stages, list) else []
        if not stages:
            stages = [{"sequence_no": 1, "stage_name": "测试阶段1"}]
        for item in contexts_by_campaign.get(campaign_id) or []:
            if not isinstance(item, Mapping) or str(item.get("status") or "") == "CANCELLED":
                continue
            raw_context = item.get("context")
            ctx = raw_context if isinstance(raw_context, Mapping) else {}
            case_code = str(item.get("case_code") or "").strip().upper()
            asset_code = str(item.get("asset_code") or "").strip().upper()
            station = station_by_id.get(str(ctx.get("station_id") or ""))
            station_code = str((station or {}).get("code") or "").strip().upper()
            version = str(item.get("version") or "")
            key = str(item.get("id") or f"{case_code}:{asset_code}:{ctx.get('cycle_id')}")
            if not key or key in seen:
                continue
            seen.add(key)
            profile = {
                "key": key,
                "label": " · ".join(
                    part for part in (case_code, version, asset_code, station_code) if part
                ),
                "case_code": case_code,
                "test_case_id": case_code,
                "test_case_code": case_code,
                "test_case_version": version,
                "version": version,
                "sample_id": asset_code,
                "asset_code": asset_code,
                "bench_id": station_code,
                "station_code": station_code,
                "campaign_id": str(ctx.get("campaign_id") or campaign_id),
                "cycle_id": str(ctx.get("cycle_id") or ""),
                "segment_id": str(ctx.get("segment_id") or ""),
                "asset_id": str(ctx.get("asset_id") or item.get("asset_id") or ""),
                "configuration_id": str(ctx.get("configuration_id") or ""),
                "test_case_version_id": str(
                    ctx.get("test_case_version_id") or item.get("test_case_version_id") or ""
                ),
                "station_id": str(ctx.get("station_id") or ""),
                "stages": stages,
                "subject_map": {},
            }
            missing = [name for name in _PROFILE_REQUIRED if not str(profile.get(name) or "").strip()]
            for name, value in (
                ("test_case_id", case_code),
                ("sample_id", asset_code),
                ("station_code", station_code),
            ):
                if not value:
                    missing.append(name)
            if not missing:
                try:
                    build_execution_code(
                        case_code, datetime.now(timezone.utc), asset_code, station_code
                    )
                except ValueError as exc:
                    missing.append(str(exc))
            profile["ready"] = not missing
            profile["missing"] = missing
            result.append(profile)
    result.sort(key=lambda item: (item["case_code"], item["sample_id"], item["key"]))
    return result


class ReliabilityPlatformClient:
    """Cookie/CSRF client for summary approval; obsolete ingestion calls fail closed."""

    def __init__(self, base_url: str, api_key: str = "", *, timeout: float = 30.0) -> None:
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url.startswith(("http://", "https://")):
            raise ValueError("platform URL must be absolute http(s)")
        self.api_key = api_key.strip()
        self.timeout = float(timeout)
        self.cookies = http.cookiejar.CookieJar()
        self._operator_opener = request.build_opener(request.HTTPCookieProcessor(self.cookies))
        self._operator_lock = threading.RLock()

    def _call(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        service_auth: bool = True,
        timeout: float | None = None,
        expect: str = "object",
        body: bytes | None = None,
    ) -> Any:
        if path.startswith("/api/v1/ingestion"):
            raise PlatformError("直接采集提交已停用，原始 CSV 必须通过核实后的专用归档与审批流程。", status=410)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Change-Reason": "factory HMI execution ingestion",
        }
        if service_auth:
            if not self.api_key:
                raise PlatformError("测试数据提交未配置服务凭据；上位机在线监控仍可使用账号登录。")
            headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            for cookie in self.cookies:
                if cookie.name == "rp1_csrf":
                    headers["X-CSRF-Token"] = cookie.value
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        if body is not None:
            if not path.startswith("/api/v1/archives/") or "/content?offset=" not in path or method != "PUT":
                raise PlatformError("原始 CSV 仅允许使用专用归档分块接口")
            if len(body) > 4 * 1024 * 1024:
                raise ValueError("归档块不得超过 4 MiB")
            headers["Content-Type"] = "application/octet-stream"
        req = request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=body if body is not None else (json.dumps(dict(payload), ensure_ascii=False).encode("utf-8") if payload is not None else None),
            headers=headers,
            method=method,
        )
        try:
            if service_auth:
                with request.urlopen(req, timeout=timeout or self.timeout) as response:
                    raw = response.read()
            else:
                with self._operator_lock:
                    with self._operator_opener.open(req, timeout=timeout or self.timeout) as response:
                        raw = response.read()
        except error.HTTPError as exc:
            raw = exc.read()
            try:
                decoded = json.loads(raw.decode("utf-8"))
                detail = decoded.get("error", decoded.get("detail", decoded))
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = raw.decode("utf-8", errors="replace")
            raise PlatformError(
                f"{method} {path} failed: {detail or exc.reason}",
                status=exc.code,
            ) from exc
        except error.URLError as exc:
            raise PlatformError(f"cannot reach reliability platform: {exc.reason}") from exc
        value = json.loads(raw.decode("utf-8")) if raw else {}
        if not isinstance(value, dict):
            raise PlatformError(f"{method} {path} returned a non-object response")
        data = value.get("data", value)
        if expect == "list":
            if not isinstance(data, list):
                raise PlatformError(f"{method} {path} returned invalid DataEnvelope")
            return data
        if not isinstance(data, dict):
            raise PlatformError(f"{method} {path} returned invalid DataEnvelope")
        return data

    def authenticate_operator(self, username: str, password: str) -> dict[str, Any]:
        return self._call(
            "POST",
            "/api/v1/auth/login",
            payload={"username": username, "password": password},
            service_auth=False,
        )

    def heartbeat_presence(self, connection_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/api/v1/hmi/presence/{connection_id}", payload=payload, service_auth=False, timeout=5)

    def disconnect_presence(self, connection_id: str) -> dict[str, Any]:
        return self._call("DELETE", f"/api/v1/hmi/presence/{connection_id}", service_auth=False, timeout=3)

    def logout_operator(self) -> None:
        try:
            self._call("POST", "/api/v1/auth/logout", service_auth=False, timeout=3)
        finally:
            self.cookies.clear()

    def submit_summary(self, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        return self._call("POST", "/api/v1/submissions", payload=payload, idempotency_key=idempotency_key, service_auth=False)

    def create_archive(self, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        return self._call("POST", "/api/v1/archives", payload=payload, idempotency_key=key, service_auth=False)

    def archive(self, archive_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/v1/archives/{archive_id}", service_auth=False)

    def archive_chunk(self, archive_id: str, offset: int, body: bytes, key: str) -> dict[str, Any]:
        return self._call("PUT", f"/api/v1/archives/{archive_id}/content?offset={offset}", body=body, idempotency_key=key, service_auth=False)

    def archive_action(self, archive_id: str, action: str, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        if action not in {"complete", "verification", "classification", "submit"}:
            raise ValueError("unsupported archive action")
        return self._call("POST", f"/api/v1/archives/{archive_id}/{action}", payload=payload, idempotency_key=key, service_auth=False)

    def submission(self, submission_id: str) -> dict[str, Any]:
        return self._call("GET", f"/api/v1/submissions/{submission_id}", service_auth=False)

    def register_execution(
        self, payload: Mapping[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            "/api/v1/ingestion/executions",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def events(
        self,
        producer_execution_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/v1/ingestion/executions/by-producer/{producer_execution_key}/events",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def telemetry(
        self,
        producer_execution_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/v1/ingestion/executions/by-producer/{producer_execution_key}/telemetry",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def artifact(
        self,
        producer_execution_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/v1/ingestion/executions/by-producer/{producer_execution_key}/artifacts",
            payload=payload,
            idempotency_key=idempotency_key,
        )

    def finish(
        self,
        producer_execution_key: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        return self._call(
            "POST",
            f"/api/v1/ingestion/executions/by-producer/{producer_execution_key}/finish",
            payload=payload,
            idempotency_key=idempotency_key,
        )


def _iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _extract_summary(bundle: Path) -> dict[str, Any]:
    # Only the bounded aggregate is read. Never extract or serialize raw.csv.
    with tarfile.open(bundle, "r:gz") as archive:
        member = archive.getmember("analysis/summary.json")
        if member.size > 65536 or not member.isfile():
            raise ValueError("local summary is invalid or too large")
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError("execution bundle is missing summary")
        value = json.loads(handle.read(65537).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("local summary must be an object")
        return value


class ReliabilityUploaderWorker:
    """Submit frozen summaries, then poll independent approval decisions."""

    def __init__(
        self,
        outbox: Outbox,
        client: ReliabilityPlatformClient,
        *,
        poll_interval_s: float = 2.0,
        presence: Any | None = None,
    ) -> None:
        self.outbox = outbox
        self.client = client
        self.poll_interval_s = poll_interval_s
        self.presence = presence
        self.station_id = "reliability-v1"
        self._identity = ""
        self._submission_lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        with self._submission_lock:
            return self._authenticate(username, password)

    def context_profiles(self) -> list[dict[str, Any]]:
        with self._submission_lock:
            if not self._identity:
                return []
            campaigns = self.client._call(
                "GET", "/api/v1/setup/campaigns", service_auth=False, expect="list"
            )
            options = self.client._call("GET", "/api/v1/setup/options", service_auth=False)
            contexts: dict[str, list[Any]] = {}
            for campaign in campaigns:
                if not isinstance(campaign, Mapping):
                    continue
                campaign_id = str(campaign.get("id") or "")
                if not campaign_id:
                    continue
                contexts[campaign_id] = self.client._call(
                    "GET",
                    f"/api/v1/setup/campaigns/{campaign_id}/contexts",
                    service_auth=False,
                    expect="list",
                )
            stations = options.get("stations") if isinstance(options.get("stations"), list) else []
            return profiles_from_setup(campaigns, contexts, stations)

    def station_options(self) -> list[dict[str, Any]]:
        with self._submission_lock:
            if not self._identity:
                return []
            options = self.client._call("GET", "/api/v1/setup/options", service_auth=False)
            return [dict(item) for item in options.get("stations", []) if isinstance(item, Mapping) and item.get("active", True)]

    def _authenticate(self, username: str, password: str) -> dict[str, Any]:
        if self._identity:
            self.clear_credentials()
        grant = self.client.authenticate_operator(username.strip(), password)
        user = grant.get("user") if isinstance(grant.get("user"), Mapping) else {}
        if user.get("must_change_password"):
            try:
                self.client.logout_operator()
            except PlatformError:
                pass
            raise PlatformError("请先在平台网页完成首次登录改密，再登录上位机。", status=403)
        if self.presence is not None:
            try:
                self.presence.connect()
            except Exception:
                self.presence.disconnect()
                try:
                    self.client.logout_operator()
                except PlatformError:
                    pass
                raise
        self._identity = str(user.get("username") or username).strip()
        return {
            "access_token": "browser-session-managed-in-memory",
            "expires_at": grant.get("expires_at", ""),
            "user": dict(user),
        }

    def clear_credentials(self) -> None:
        with self._submission_lock:
            self._clear_credentials()

    def _clear_credentials(self) -> None:
        self._identity = ""
        if self.presence is not None:
            self.presence.disconnect()
            try:
                self.client.logout_operator()
            except PlatformError:
                pass

    def authorize_service_identity(self, identity: str = "SERVICE_API") -> None:
        self._identity = identity.strip() or "SERVICE_API"

    def set_presence(self, _payload: Mapping[str, Any]) -> None:
        return

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rp1-reliability-uploader",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    def process_once(self) -> int:
        with self._submission_lock:
            return self._process_once()

    def _process_once(self) -> int:
        if not self._identity:
            return 0
        count = 0
        for item in self.outbox.due(limit=10, owner=self._identity):
            if not item.submission_owner or item.submission_owner != self._identity:
                continue
            try:
                self._process(item)
            except (PlatformError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
                if item.manifest.get("archive_protocol") == "reliability_v1_csv" and isinstance(exc, PlatformError) and exc.status in {400, 409, 422}:
                    self.outbox.update(item.execution_uuid, state="needs_verification", last_error=str(exc), review_reason="平台校验未通过，请核实后重新提交", next_attempt_at=0)
                else:
                    self.outbox.fail(item.execution_uuid, str(exc))
            count += 1
        return count

    @staticmethod
    def _validate_manifest(manifest: Mapping[str, Any]) -> None:
        missing = [name for name in _PROFILE_REQUIRED if not str(manifest.get(name) or "").strip()]
        if missing:
            raise ValueError(
                "reliability platform context is incomplete: " + ", ".join(missing)
            )
        build_execution_code(
            str(manifest.get("test_case_id") or ""), _iso(str(manifest["started_at"])),
            str(manifest.get("sample_id") or ""), str(manifest.get("bench_id") or ""),
        )

    def _process(self, item: OutboxItem) -> None:
        if not item.submission_owner or item.submission_owner != self._identity:
            return  # Legacy/unclaimed queues require an explicit signed-in submission.
        manifest = item.manifest
        if manifest.get("archive_protocol") == "reliability_v1_csv":
            self._process_archive(item)
            return
        self._validate_manifest(manifest)
        producer = str(manifest["execution_uuid"])
        if item.submission_id and item.remote_state == "PENDING":
            receipt = self.client.submission(item.submission_id)
        else:
            if item.submission_payload:
                request_payload = json.loads(item.submission_payload)
            else:
                bundle = Path(item.bundle_path).expanduser().resolve(strict=True)
                summary = _extract_summary(bundle)
                verdict = summary.get("verdict") or {}
                timing = summary.get("timing") or {}
                payload = {
                    **{name: manifest[name] for name in _PROFILE_REQUIRED},
                    "execution_uuid": producer,
                    "started_at": manifest["started_at"], "ended_at": manifest["ended_at"],
                    "active_seconds": float(timing.get("active_seconds") or 0),
                    "completed_cycles": int(timing.get("completed_cycles") or 0),
                    "source_status": str(manifest.get("verified_status") or manifest["status"]),
                    "data_quality": "VALID" if float((summary.get("quality") or {}).get("sample_completeness") or 0) >= .99 else "PARTIAL",
                    "clock_quality": "VALID",
                    "exception_count": sum(int(m.get("fault_count") or 0) for m in summary.get("joint_metrics") or []),
                    "summary": (
                        str(manifest.get("operator_summary") or "").strip()
                        or "; ".join(str(reason) for reason in verdict.get("reasons") or [])
                    )[:500],
                    "evidence_sha256": _sha256(bundle),
                }
                request_payload = {
                    "summary": payload, "expected_revision": item.submission_revision,
                    "reason": (
                        str(manifest.get("submission_reason") or "").strip()[:300]
                        or ("提交工位本地测试摘要" if not item.submission_revision else "根据审核意见重新提交本地测试摘要")
                    ),
                }
                # Freeze the exact request BEFORE sending. A lost response or a
                # local report rebuild must not change an idempotent retry.
                self.outbox.update(producer, submission_payload=json.dumps(request_payload, ensure_ascii=False, sort_keys=True))
            receipt = self.client.submit_summary(request_payload, f"{producer}:summary:{item.submission_revision + 1}")
        if not isinstance(receipt, Mapping):
            raise ValueError("审批回执格式无效")
        from uuid import UUID
        submission_id = str(UUID(str(receipt["id"])))
        if str(receipt.get("execution_uuid")) != producer or receipt.get("submitter") != self._identity:
            raise ValueError("审批回执与本地记录或提交账号不一致")
        status = str(receipt["status"])
        if status not in {"PENDING", "APPROVED", "REJECTED"}:
            raise ValueError("未知审批状态")
        revision = int(receipt["revision"])
        if revision < max(1, item.submission_revision):
            raise ValueError("审批回执版本过期")
        code = str(receipt.get("execution_code") or "")
        if status == "APPROVED":
            expected = build_execution_code(str(manifest["test_case_id"]), _iso(str(manifest["started_at"])), str(manifest["sample_id"]), str(manifest["bench_id"]))
            if not re.fullmatch(re.escape(expected) + r"(?:_E[0-9]+)?", code):
                raise ValueError("平台正式执行单号与本地上下文不一致")
        latest = (receipt.get("revisions") or [{}])[-1]
        self.outbox.update(producer, submission_id=submission_id, submission_revision=revision,
            state={"PENDING": "pending_review", "APPROVED": "approved", "REJECTED": "rejected"}[status],
            remote_state=status, execution_code=code,
            review_reason=str(latest.get("review_reason") or "等待审批；尚未进入正式查询和统计"),
            next_attempt_at=time.time() + 10 if status == "PENDING" else 0, last_error="")

    def _process_archive(self, item: OutboxItem) -> None:
        """Resume only an explicitly verified, account-bound CSV; never send a bundle."""
        from uuid import UUID
        producer = str(UUID(item.execution_uuid))
        manifest = item.manifest
        csv_path = Path(str(manifest["csv_path"])).expanduser().resolve(strict=True)
        evidence = next((entry for entry in manifest.get("files", []) if entry.get("path") == "telemetry/raw.csv"), None)
        if not evidence or not isinstance(evidence.get("size_bytes"), int) or not re.fullmatch(r"[0-9a-f]{64}", str(evidence.get("sha256") or "")):
            raise ValueError("缺少已核实原始 CSV 的冻结证据，不能归档")
        verified_size = evidence["size_bytes"]
        verified_sha256 = evidence["sha256"]
        # The enqueue-time manifest binds the human verification to these bytes.
        # The first worker attempt must not accept a fresh hash of a changed file.
        if csv_path.stat().st_size != verified_size or _sha256(csv_path) != verified_sha256:
            raise ValueError("原始 CSV 与已核实冻结证据不一致，不能归档")
        frozen = json.loads(item.submission_payload) if item.submission_payload else {}
        if not frozen:
            from factory_hmi.desktop.operator_review import analysis_text
            summary = _extract_summary(Path(item.bundle_path))
            timing, quality, verdict = (summary.get(key) or {} for key in ("timing", "quality", "verdict"))
            config_path = csv_path.parent.parent / "config/execution.json"
            context = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
            station_config = csv_path.parent.parent / "config/station.yaml"
            config = yaml.safe_load(station_config.read_text(encoding="utf-8")) if station_config.is_file() else {}
            acceptance = (config or {}).get("factory_hmi_acceptance") or {}
            allowed = {"criteria_version", "required_active_seconds", "required_cycles", "min_sample_completeness", "max_gap_seconds", "max_temperature_rise_c", "max_tracking_rms_rad", "max_torque_abs_nm"}
            criteria = {key: value for key, value in acceptance.items() if key in allowed}
            criteria["criteria_version"] = str(criteria.get("criteria_version") or "UNCONFIGURED")
            if context.get("record_rate_hz"):
                criteria["record_rate_hz"] = context["record_rate_hz"]
            clock_missing = manifest.get("timestamp_provenance") == "missing"
            machine = str(manifest.get("machine_status") or "unknown")
            machine = {"fault": "failed", "passed": "completed", "blocked": "interrupted"}.get(machine, machine)
            if machine not in {"completed", "failed", "stopped", "interrupted", "unknown"}:
                machine = "unknown"
            auto = "unconfigured" if criteria["criteria_version"] == "UNCONFIGURED" else str(verdict.get("status") or "blocked")
            create = {
                "execution_uuid": producer, "test_name": manifest["test_name"], "actual_object": manifest["actual_object"],
                "station_label": str(manifest.get("bench_id") or "unknown"), "origin": manifest.get("origin", "HMI"),
                "filename": csv_path.name, "size_bytes": verified_size, "sha256": verified_sha256,
                "facts": {"started_at": None if clock_missing else manifest["started_at"], "ended_at": None if clock_missing else manifest["ended_at"],
                    "active_seconds": float(timing.get("active_seconds") or 0), "completed_cycles": int(timing.get("completed_cycles") or 0),
                    "machine_status": machine, "automatic_verdict": auto,
                    "data_quality": "VALID" if float(quality.get("sample_completeness") or 0) >= .99 and machine == "completed" else "PARTIAL",
                    "clock_quality": "MISSING" if clock_missing else "VALID", "exception_count": sum(int(j.get("fault_count") or 0) for j in summary.get("joint_metrics") or []),
                    "summary": analysis_text(summary)[:500]},
                "acceptance": criteria,
            }
            if manifest.get("recording_scope") == "platform" and all(manifest.get(key) for key in _PROFILE_REQUIRED):
                create["classification"] = {key: manifest[key] for key in _PROFILE_REQUIRED}
            frozen = {"create": create, "review": dict(manifest["operator_review"])}
            self.outbox.update(producer, submission_payload=json.dumps(frozen, ensure_ascii=False, sort_keys=True))
        create = frozen["create"]
        # Any post-verification change to source CSV fails closed, including retries.
        if csv_path.stat().st_size != create["size_bytes"] or _sha256(csv_path) != create["sha256"]:
            raise ValueError("原始 CSV 已变更，冻结归档不能继续")
        detail = self.client.archive(item.upload_id) if item.upload_id else self.client.create_archive(create, producer + ":archive")
        archive_id = str(UUID(str(detail["id"])))
        if str(detail.get("execution_uuid")) != producer:
            raise ValueError("归档回执执行身份不一致")
        if item.submission_id and item.remote_state == "PENDING":
            receipt = self.client.submission(item.submission_id)
            if str(receipt.get("execution_uuid")) != producer or receipt.get("submitter") != self._identity:
                raise ValueError("审批回执与执行身份或提交账号不一致")
            status = str(receipt["status"])
            if status not in {"PENDING", "APPROVED", "REJECTED"}:
                raise ValueError("未知审批状态")
            self.outbox.update(producer, state={"PENDING": "pending_review", "APPROVED": "approved", "REJECTED": "rejected"}[status],
                remote_state=status, submission_revision=int(receipt["revision"]), execution_code=str(receipt.get("execution_code") or ""),
                review_reason=str((receipt.get("revisions") or [{}])[-1].get("review_reason") or "等待审批"),
                next_attempt_at=time.time() + 10 if status == "PENDING" else 0, last_error="")
            return
        self.outbox.update(producer, upload_id=archive_id, state="uploading")
        processing_error = str(detail.get("processing_error") or "中央 CSV 解析失败")
        if detail.get("processing_state") == "FAILED" and processing_error != "CHECKSUM_MISMATCH" and item.state != "local_completed":
            # Parsing failures need an explicit operator retry (for example after
            # a parser repair). Do not repeatedly requeue the same invalid CSV.
            self.outbox.update(producer, state="processing_failed", last_error=processing_error,
                review_reason="中央 CSV 解析失败；原件已保留，可在问题修复后点击重试归档", next_attempt_at=0)
            return
        offset = int(detail.get("received_bytes") or 0)
        if offset < 0 or offset > create["size_bytes"]:
            raise ValueError("归档回执字节偏移无效")
        with csv_path.open("rb") as handle:
            handle.seek(offset)
            while chunk := handle.read(4 * 1024 * 1024):
                receipt = self.client.archive_chunk(archive_id, offset, chunk, f"{producer}:chunk:{offset}")
                received = int(receipt["received_bytes"])
                if received != offset + len(chunk):
                    raise ValueError("归档分块回执偏移不一致")
                offset = received
                self.outbox.update(producer, next_chunk_index=offset)
        if detail.get("processing_state") != "READY":
            # The worker resets corrupt content to offset 0. After retransmitting
            # the unchanged frozen CSV, complete must re-run SHA256 validation.
            # Explicit retries also requeue recoverable non-checksum failures.
            detail = self.client.archive_action(archive_id, "complete", {}, producer + ":complete")
            if detail.get("processing_state") == "FAILED":
                error_text = str(detail.get("processing_error") or "中央 CSV 解析失败")
                checksum_failed = error_text == "CHECKSUM_MISMATCH"
                self.outbox.update(producer, state="validating" if checksum_failed else "processing_failed",
                    next_attempt_at=time.time() + 3 if checksum_failed else 0, last_error=error_text,
                    review_reason="校验失败，恢复后重新上传原始 CSV" if checksum_failed else "解析失败；原件已保留，修复后可显式重试")
                return
            if detail.get("processing_state") != "READY":
                self.outbox.update(producer, state="validating", next_attempt_at=time.time() + 3, last_error="")
                return
        central_analysis = (detail.get("manifest") or {}).get("analysis")
        if isinstance(central_analysis, Mapping) and central_analysis.get("verdict") in {"passed", "failed", "blocked", "unconfigured"}:
            self.outbox.record_central_analysis(producer, central_analysis)
        if "verification" not in frozen:
            review = frozen["review"]
            frozen["verification"] = {"expected_revision": int(detail["revision"]), "tester": review["tester"],
                "station_id": review["station_id"], "verdict": review["verified_status"], "notes": review["notes"],
                "discrepancy_reason": review.get("discrepancy_reason", ""), "reason": manifest["submission_reason"]}
            self.outbox.update(producer, submission_payload=json.dumps(frozen, ensure_ascii=False, sort_keys=True))
        verification = frozen["verification"]
        detail = self.client.archive_action(archive_id, "verification", verification,
            f"{producer}:verification:{frozen['review'].get('revision', 1)}")
        if detail.get("classification_state") != "CLASSIFIED":
            self.outbox.update(producer, state="awaiting_classification", remote_state="UNCLASSIFIED", review_reason="中央归档已完成，等待补齐正式用例关联", last_error="", next_attempt_at=0)
            return
        # Preserve the planned station in the first classification. A corrected
        # actual station is a new auditable classification after verification.
        classification = detail.get("classification") or create.get("classification") or {}
        corrected_station = frozen["review"]["station_id"]
        if "classification" not in frozen and str(classification.get("station_id") or "") != corrected_station:
            missing = [key for key in _PROFILE_REQUIRED if not classification.get(key)]
            if missing:
                raise ValueError("正式归类上下文不完整，无法记录工位修正：" + "、".join(missing))
            frozen["classification"] = {"expected_revision": int(detail["revision"]),
                "context": {**{key: str(classification[key]) for key in _PROFILE_REQUIRED}, "station_id": corrected_station},
                "reason": manifest["submission_reason"]}
            self.outbox.update(producer, submission_payload=json.dumps(frozen, ensure_ascii=False, sort_keys=True))
        if "classification" in frozen:
            detail = self.client.archive_action(archive_id, "classification", frozen["classification"],
                f"{producer}:station:{frozen['review'].get('revision', 1)}")
        if "submit" not in frozen:
            frozen["submit"] = {"expected_revision": int(detail["revision"]),
                "expected_submission_revision": int(detail.get("submission_revision") or item.submission_revision), "reason": manifest["submission_reason"]}
            self.outbox.update(producer, submission_payload=json.dumps(frozen, ensure_ascii=False, sort_keys=True))
        submit = frozen["submit"]
        detail = self.client.archive_action(archive_id, "submit", submit, f"{producer}:submit:{submit['expected_submission_revision'] + 1}")
        status = str(detail.get("submission_status") or "PENDING")
        self.outbox.update(producer, state={"PENDING": "pending_review", "APPROVED": "approved", "REJECTED": "rejected"}.get(status, "pending_review"),
            remote_state=status, submission_id=str(detail.get("submission_id") or ""), submission_revision=int(detail.get("submission_revision") or 0),
            execution_code=str(detail.get("execution_code") or ""), review_reason="归档完成，等待审批" if status == "PENDING" else status,
            next_attempt_at=time.time() + 10 if status == "PENDING" else 0, last_error="")

    def _run(self) -> None:
        while not self._stop.is_set():
            self.process_once()
            self._wake.wait(self.poll_interval_s)
            self._wake.clear()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def reliability_worker_from_environment(outbox: Outbox, *, snapshot_provider: Any = None, on_execution: Any = None) -> ReliabilityUploaderWorker:
    key_file = os.environ.get("RP1_RELIABILITY_SERVICE_KEY_FILE", "").strip()
    try:
        api_key = read_secret(key_file) if key_file else os.environ.get("RP1_RELIABILITY_SERVICE_KEY", "").strip()
    except FileNotFoundError:
        api_key = ""
    client = ReliabilityPlatformClient(
        os.environ.get("RP1_FACTORY_PLATFORM_URL", "http://127.0.0.1:8080"),
        api_key,
    )
    from .presence import HmiPresencePublisher

    presence = HmiPresencePublisher(
        client, data_root=outbox.path.parent.parent,
        snapshot_provider=snapshot_provider or (lambda: {"runs": [], "plc": {}}),
        on_execution=on_execution,
    )
    return ReliabilityUploaderWorker(outbox, client, presence=presence)


__all__ = [
    "DEFAULT_PROFILE_PATH",
    "ReliabilityPlatformClient",
    "ReliabilityUploaderWorker",
    "load_test_profiles",
    "profiles_from_setup",
    "reliability_worker_from_environment",
]
