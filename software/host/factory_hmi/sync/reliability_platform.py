"""Independent Factory HMI bundle bridge to the reliability ingestion API.

Raw bundles stay local; canonical telemetry and artifact metadata are sent
through a dedicated durable queue, independently of desktop approvals.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import tarfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib import error, parse, request
from uuid import UUID

import yaml

from .outbox import Outbox, OutboxItem
from .uploader import PlatformError


CONTEXT_IDS = (
    "campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id",
    "test_case_version_id", "station_id",
)
MAX_POINTS = 5000
MAX_MEMBERS = 128
MAX_JSON_BYTES = 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 16 * 1024**3
_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class VerifiedBundle:
    path: Path
    sha256: str
    manifest: dict[str, Any]
    summary: dict[str, Any]
    rows: list[dict[str, str]]
    raw_row_count: int
    recording_is_synthetic: bool = False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(value: Any) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("bundle timestamps must include a UTC offset")
    return parsed.astimezone(timezone.utc)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _member_path(name: str) -> str:
    path = PurePosixPath(name)
    if (
        not name or not _PATH_PATTERN.fullmatch(name) or path.is_absolute()
        or any(part in {".", ".."} for part in name.split("/"))
        or name != path.as_posix()
    ):
        raise ValueError("bundle contains an unsafe archive path")
    return name


def _json_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> dict[str, Any]:
    if not member.isfile() or member.size > MAX_JSON_BYTES:
        raise ValueError("bundle JSON is not a bounded regular file")
    handle = archive.extractfile(member)
    if handle is None:
        raise ValueError("bundle JSON cannot be read")
    with handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("bundle JSON must contain an object")
    return value


def _read_sampled_rows(
    archive: tarfile.TarFile, member: tarfile.TarInfo,
) -> tuple[list[dict[str, str]], int]:
    """Stream CSV twice; memory stays bounded even for long aging recordings."""
    def reader() -> io.TextIOWrapper:
        handle = archive.extractfile(member)
        if handle is None:
            raise ValueError("bundle telemetry cannot be read")
        return io.TextIOWrapper(handle, encoding="utf-8", newline="")

    with reader() as handle:
        raw = csv.DictReader(handle)
        fields = raw.fieldnames or []
        if "time_s" not in fields or len(fields) != len(set(fields)):
            raise ValueError("telemetry requires unique headers including time_s")
        count = sum(1 for _ in raw)
    step = max(1, math.ceil(max(0, count - 1) / (MAX_POINTS - 1)))
    with reader() as handle:
        rows = [row for index, row in enumerate(csv.DictReader(handle)) if index % step == 0]
    return rows, count


def inspect_bundle(path: Path | str) -> VerifiedBundle:
    """Check paths, types, size and every declared SHA-256 without extracting."""
    supplied = Path(path).expanduser()
    if supplied.is_symlink() or not supplied.is_file():
        raise ValueError("bundle must be a regular file, not a symbolic link")
    bundle = supplied.resolve(strict=True)
    compressed_hash = _sha256(bundle)
    with tarfile.open(bundle, "r:gz") as archive:
        members: dict[str, tarfile.TarInfo] = {}
        total_size = 0
        for member in archive:
            name = _member_path(member.name.rstrip("/") if member.isdir() else member.name)
            if name in members or not (member.isfile() or member.isdir()):
                raise ValueError("bundle contains duplicate paths or non-regular files")
            members[name] = member
            total_size += member.size
            if len(members) > MAX_MEMBERS or total_size > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("bundle exceeds the bridge's archive size limits")
        required = {"manifest.json", "analysis/summary.json", "telemetry/raw.csv"}
        if not required <= members.keys():
            raise ValueError("bundle is missing manifest, summary or telemetry")
        manifest = _json_member(archive, members["manifest.json"])
        if manifest.get("bundle_format") != "rp1-execution-bundle" or manifest.get("schema_version") != "1.0":
            raise ValueError("only Factory execution bundle v1 is supported")
        UUID(str(manifest.get("execution_uuid", "")))
        if not str(manifest.get("test_id") or "").strip():
            raise ValueError("bundle test_id is required")
        if manifest.get("status") not in {"passed", "failed", "blocked"}:
            raise ValueError("bundle verdict must be passed, failed or blocked")
        start, end = _utc(manifest.get("started_at")), _utc(manifest.get("ended_at"))
        if end < start:
            raise ValueError("bundle ends before it starts")
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("bundle manifest requires a file inventory")
        declared: set[str] = set()
        for item in files:
            if not isinstance(item, Mapping):
                raise ValueError("bundle file inventory contains a non-object")
            name = _member_path(str(item.get("path", "")))
            size, digest = item.get("size_bytes"), str(item.get("sha256", ""))
            if name in declared or name == "manifest.json" or name not in members:
                raise ValueError("bundle file inventory is inconsistent")
            member = members[name]
            if not member.isfile() or type(size) is not int or size != member.size or not _SHA_PATTERN.fullmatch(digest):
                raise ValueError("bundle file size, type or hash is invalid")
            handle = archive.extractfile(member)
            assert handle is not None
            computed = hashlib.sha256()
            with handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    computed.update(chunk)
            if computed.hexdigest() != digest:
                raise ValueError("bundle file SHA-256 mismatch")
            declared.add(name)
        actual_files = {name for name, member in members.items() if member.isfile()} - {"manifest.json"}
        if declared != actual_files or not {"analysis/summary.json", "telemetry/raw.csv"} <= declared:
            raise ValueError("bundle contains undeclared or missing files")
        summary = _json_member(archive, members["analysis/summary.json"])
        if summary.get("execution_uuid") != manifest["execution_uuid"] or summary.get("test_id") != manifest["test_id"]:
            raise ValueError("bundle summary identity differs from its manifest")
        verdict = summary.get("verdict")
        if not isinstance(verdict, Mapping) or verdict.get("status") != manifest["status"]:
            raise ValueError("bundle summary verdict differs from its manifest")
        timing = summary.get("timing")
        if not isinstance(timing, Mapping):
            raise ValueError("bundle summary timing is missing")
        if _utc(timing.get("started_at")) != start or _utc(timing.get("ended_at")) != end:
            raise ValueError("summary timestamps differ from the manifest")
        active = _finite(timing.get("active_seconds"))
        if active is None or active < 0 or active > (end - start).total_seconds() + 1e-6:
            raise ValueError("bundle active duration is missing or exceeds elapsed time")
        software = manifest.get("software") or {}
        synthetic = "synthetic" in str(software.get("hmi_version", "")).lower()
        station_member = members.get("config/station.yaml")
        if station_member is not None and station_member.size <= MAX_JSON_BYTES:
            station_file = archive.extractfile(station_member)
            assert station_file is not None
            with station_file:
                station = yaml.safe_load(station_file) or {}
            if isinstance(station, Mapping):
                synthetic |= station.get("backend") == "fake" or station.get("source_kind") == "synthetic"
        rows, count = _read_sampled_rows(archive, members["telemetry/raw.csv"])
    return VerifiedBundle(bundle, compressed_hash, manifest, summary, rows, count, synthetic)


def bridge_manifest(
    bundle: VerifiedBundle, context: Mapping[str, Any], subject_map: Mapping[str, str],
    *, source_kind: str = "synthetic", evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Whitelist context fields: credentials never enter the queue or metadata."""
    if source_kind not in {"synthetic", "real"}:
        raise ValueError("source_kind must be synthetic or real")
    if source_kind == "real":
        if context.get("source_kind") == "synthetic" or context.get("backend") == "fake":
            raise ValueError("synthetic database context cannot be relabeled as real")
        if (
            not evidence or evidence.get("backend") != "motors_py"
            or evidence.get("measurement_origin") != "physical_robot"
            or not str(evidence.get("recording_reference") or "").strip()
            or not str(evidence.get("confirmed_by") or "").strip()
        ):
            raise ValueError("real ingestion requires explicit physical-recording evidence")
        if bundle.recording_is_synthetic or evidence.get("source_kind") == "synthetic":
            raise ValueError("a synthetic/fake bundle cannot be relabeled as real")
    result = {
        name: str(bundle.manifest[name])
        for name in ("execution_uuid", "test_id", "started_at", "ended_at", "status")
    }
    for name in CONTEXT_IDS:
        result[name] = str(UUID(str(context.get(name) or "")))
    if any(not isinstance(joint, str) or not isinstance(subject, str) for joint, subject in subject_map.items()):
        raise ValueError("subject_map requires string keys and values")
    mapping = {joint.strip(): subject.strip() for joint, subject in subject_map.items()}
    if not mapping or any(not joint or not subject or len(subject) > 100 for joint, subject in mapping.items()):
        raise ValueError("subject_map must contain non-empty joint -> catalog subject codes")
    if len(set(mapping.values())) != len(mapping):
        raise ValueError("different HMI joints must not map to the same catalog subject")
    verdict = bundle.summary.get("verdict") or {}
    rules = verdict.get("rules") or []
    criteria_complete = (
        verdict.get("criteria_version") not in {None, "", "UNCONFIGURED"}
        and bool(rules)
        and all(isinstance(rule, Mapping) and rule.get("result") in {"passed", "failed"} for rule in rules)
    )
    result.update(
        subject_map=mapping, source_kind=source_kind,
        stage_code=str(context.get("stage_code") or "FACTORY-AGING"),
        stage_name=str(context.get("stage_name") or "Factory HMI 老化记录"),
        bundle_format="rp1-execution-bundle", bundle_sha256=bundle.sha256,
        criteria_complete=criteria_complete,
    )
    if source_kind == "synthetic" or (result["status"] == "passed" and not criteria_complete):
        result["status"] = "blocked"
    if evidence:
        # Only an integrity digest is persisted; no arbitrary evidence content.
        result["evidence_sha256"] = hashlib.sha256(
            json.dumps(dict(evidence), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
    return result


def enqueue_bundle(outbox: Outbox, bundle: VerifiedBundle, manifest: Mapping[str, Any]) -> OutboxItem:
    existing = outbox.get(str(manifest["execution_uuid"]))
    if existing is not None:
        if existing.manifest != dict(manifest):
            raise ValueError("execution identity already exists with different bundle or context")
        return existing
    return outbox.enqueue(manifest, bundle.path)


_METRICS = (
    ("cmd_pos_rad", "JOINT-TARGET-POSITION", "deg", math.degrees),
    ("pos_rad", "JOINT-ACTUAL-POSITION", "deg", math.degrees),
    ("tracking_error_rad", "JOINT-TRACKING-ERROR", "deg", math.degrees),
    ("spd_rad_s", "JOINT-VELOCITY", "deg/s", math.degrees),
    ("torque_nm", "JOINT-TORQUE", "N·m", float),
    ("temp_c", "JOINT-TEMPERATURE", "°C", float),
    ("current_a", "JOINT-CURRENT", "A", float),
)


def build_telemetry_batches(
    rows: list[dict[str, str]], *, manifest: Mapping[str, Any], stage_id: str,
    producer_key: str,
) -> list[dict[str, Any]]:
    """Map recorded values; derive error only where target and feedback exist."""
    if len(rows) > MAX_POINTS:
        step = math.ceil(len(rows) / MAX_POINTS)
        rows = rows[::step][:MAX_POINTS]
    start, end = _utc(manifest["started_at"]), _utc(manifest["ended_at"])
    duration = (end - start).total_seconds()
    samples: list[tuple[dict[str, str], float]] = []
    last_time = -1.0
    for row in rows:
        elapsed = _finite(row.get("time_s"))
        if elapsed is None or elapsed < 0 or elapsed > duration or elapsed <= last_time:
            continue
        last_time = elapsed
        samples.append((row, elapsed))
    series: list[dict[str, Any]] = []
    for joint, subject in manifest["subject_map"].items():
        for suffix, metric, unit, converter in _METRICS:
            points: list[dict[str, Any]] = []
            for row, elapsed in samples:
                if suffix == "tracking_error_rad":
                    actual, target = _finite(row.get(f"{joint}_pos_rad")), _finite(row.get(f"{joint}_cmd_pos_rad"))
                    value = None if actual is None or target is None else actual - target
                else:
                    value = _finite(row.get(f"{joint}_{suffix}"))
                if value is None:
                    continue
                converted = _finite(converter(value))
                if converted is None:
                    continue
                points.append({
                    "sample_index": len(points),
                    "observed_at": (start + timedelta(seconds=elapsed)).isoformat(),
                    "value": converted, "quality_status": "VALID",
                })
            if not points:
                continue
            interval = 10.0
            if len(points) > 1:
                interval = (_utc(points[-1]["observed_at"]) - _utc(points[0]["observed_at"])).total_seconds() * 1000 / (len(points) - 1)
            quality = "VALID" if len(points) == len(rows) else "PARTIAL"
            reference = {"bundle_file": "telemetry/raw.csv", "source_column": f"{joint}_{suffix}"}
            if suffix == "tracking_error_rad":
                reference = {
                    "bundle_file": "telemetry/raw.csv", "derivation": "feedback - target",
                    "source_columns": [f"{joint}_pos_rad", f"{joint}_cmd_pos_rad"],
                }
            series.append({
                "producer_series_key": f"{producer_key}:{metric}:{subject}",
                "metric_code": metric, "metric_version": "1.0.0",
                "subject_code": subject, "canonical_unit": unit,
                "series_kind": "STEADY_STATE_WINDOW", "started_at": points[0]["observed_at"],
                "ended_at": points[-1]["observed_at"],
                "sampling_interval_ms": max(10.0, min(3_600_000.0, interval)),
                "downsample_method": "UNIFORM", "quality_status": quality,
                "raw_data_reference": reference, "points": points,
            })
    return [{
        "producer_key": f"{producer_key}:telemetry:{offset // 20:04d}",
        "stage_id": stage_id, "series": series[offset:offset + 20],
    } for offset in range(0, len(series), 20)]


class ReliabilityPlatformClient:
    """SERVICE-authenticated DataEnvelope client; safe, bounded error messages."""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30.0) -> None:
        parsed = parse.urlsplit(base_url.strip().rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("platform URL must be absolute HTTP(S), without credentials or query")
        if not api_key.strip():
            raise ValueError("reliability platform SERVICE API key is required")
        self.base_url, self.api_key, self.timeout = base_url.strip().rstrip("/"), api_key.strip(), timeout

    def _call(self, path: str, payload: Mapping[str, Any], idempotency_key: str) -> dict[str, Any]:
        req = request.Request(
            self.base_url + path,
            data=json.dumps(dict(payload), ensure_ascii=False, allow_nan=False).encode("utf-8"),
            headers={
                "Accept": "application/json", "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}", "Idempotency-Key": idempotency_key,
                "X-Change-Reason": "Factory HMI independent bundle ingestion",
            }, method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read(MAX_JSON_BYTES + 1)
        except error.HTTPError as exc:
            raise PlatformError(f"reliability ingestion returned HTTP {exc.code}", status=exc.code) from None
        except error.URLError:
            raise PlatformError("cannot reach reliability ingestion; retained for retry") from None
        if len(raw) > MAX_JSON_BYTES:
            raise PlatformError("reliability response exceeds size limit")
        try:
            value = json.loads(raw)
            data = value.get("data", value)
        except (ValueError, AttributeError):
            raise PlatformError("reliability ingestion returned invalid DataEnvelope") from None
        if not isinstance(data, dict):
            raise PlatformError("reliability ingestion returned a non-object envelope")
        return data

    def register_execution(self, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        return self._call("/api/v1/ingestion/executions", payload, key)

    def _execution_call(self, producer: str, action: str, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        path = f"/api/v1/ingestion/executions/by-producer/{parse.quote(producer, safe='')}/{action}"
        return self._call(path, payload, key)

    def telemetry(self, producer: str, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        return self._execution_call(producer, "telemetry", payload, key)

    def events(self, producer: str, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        return self._execution_call(producer, "events", payload, key)

    def artifact(self, producer: str, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        return self._execution_call(producer, "artifacts", payload, key)

    def finish(self, producer: str, payload: Mapping[str, Any], key: str) -> dict[str, Any]:
        return self._execution_call(producer, "finish", payload, key)


class ReliabilityUploaderWorker:
    """Replay ordered requests using stable keys after interruption or failure.

    Use a dedicated bridge outbox. The outbox's ``approved`` terminal state is a
    SQLite implementation detail here; it means ingestion accepted, no review.
    """

    def __init__(self, outbox: Outbox, client: ReliabilityPlatformClient) -> None:
        self.outbox, self.client = outbox, client

    def process_once(self) -> int:
        accepted = 0
        for item in self.outbox.due(limit=10):
            try:
                self._process(item)
            except (PlatformError, OSError, ValueError, KeyError, tarfile.TarError) as exc:
                self.outbox.fail(item.execution_uuid, str(exc))
            else:
                accepted += 1
        return accepted

    def _process(self, item: OutboxItem) -> None:
        manifest, producer = item.manifest, item.execution_uuid
        bundle = inspect_bundle(item.bundle_path)
        if bundle.sha256 != manifest["bundle_sha256"] or bundle.manifest["execution_uuid"] != producer:
            raise ValueError("queued bundle changed; ingestion stopped")
        preview = build_telemetry_batches(bundle.rows, manifest=manifest, stage_id="preview", producer_key=producer)
        if not preview:
            raise ValueError("no finite mapped telemetry; ingestion stopped")
        source = manifest["source_kind"]
        registration = self.client.register_execution({
            "producer_key": producer, **{key: manifest[key] for key in CONTEXT_IDS},
            "started_at": manifest["started_at"], "source_status": "running",
            "stage_code": manifest["stage_code"], "stage_name": manifest["stage_name"],
            "source_metadata": {
                "source_kind": source, "hmi_test_id": manifest["test_id"],
                "bridge": "factory-bundle-v1", "bundle_sha256": bundle.sha256,
                "physical_evidence_attested": bool(manifest.get("evidence_sha256")),
                "criteria_complete": manifest["criteria_complete"],
            },
        }, f"{producer}:register")
        stage_id, execution_id = str(registration["stage_id"]), str(registration["execution_id"])
        self.outbox.update(producer, state="uploading", submission_id=execution_id, upload_id=stage_id, remote_state="RUNNING")
        batches = build_telemetry_batches(bundle.rows, manifest=manifest, stage_id=stage_id, producer_key=producer)
        for index, batch in enumerate(batches):
            self.client.telemetry(producer, batch, f"{producer}:telemetry:{index:04d}")
        verdict = bundle.summary.get("verdict") or {}
        status = "blocked" if source == "synthetic" else manifest["status"]
        self.client.events(producer, {
            "producer_key": f"{producer}:events", "events": [{
                "producer_event_key": f"{producer}:verdict", "event_type": "FACTORY_AGING_VERDICT",
                "occurred_at": manifest["ended_at"], "payload": {
                    "source_kind": source, "status": status, "local_report_status": bundle.manifest["status"],
                    "criteria_version": verdict.get("criteria_version"),
                    "criteria_complete": manifest["criteria_complete"],
                    "reasons": verdict.get("reasons") or [],
                    "note": "synthetic software acceptance; no robot reliability verdict" if source == "synthetic" else "physical evidence supplied by operator",
                }, "clock_quality": "VALID", "data_quality": "VALID",
            }],
        }, f"{producer}:events")
        self.client.artifact(producer, {
            "producer_key": f"{producer}:bundle", "artifact_kind": "FACTORY_EXECUTION_BUNDLE",
            "availability_status": "MISSING", "source_location": "retained on HMI workstation",
            "file_name": bundle.path.name, "mime_type": "application/gzip",
            "size_bytes": bundle.path.stat().st_size, "sha256": bundle.sha256,
            "metadata": {
                "reason": "platform binary upload endpoint is not available",
                "bundle_format": "rp1-execution-bundle", "source_kind": source,
            },
        }, f"{producer}:artifact")
        quality = bundle.summary.get("quality") or {}
        partial = (_finite(quality.get("sample_completeness")) or 0.0) < 0.99 or any(
            series["quality_status"] != "VALID" for batch in batches for series in batch["series"]
        )
        summary = "Factory HMI bundle ingested; binary stays local."
        if source == "synthetic":
            summary += " Synthetic/fake software acceptance only; reliability verdict inconclusive."
        elif not manifest["criteria_complete"] and status == "blocked":
            summary += " Acceptance criteria are incomplete or not evaluated; verdict inconclusive."
        self.client.finish(producer, {
            "producer_key": f"{producer}:finish", "source_status": status,
            "finished_at": manifest["ended_at"], "active_seconds": bundle.summary["timing"]["active_seconds"],
            "outcome": {"passed": "PASSED", "failed": "FAILED", "blocked": "INCONCLUSIVE"}[status],
            "termination_kind": "UNKNOWN" if status == "blocked" else "NORMAL",
            "summary": summary, "issues": "", "exception_count": sum(
                max(0, int(row.get("fault_count") or 0)) for row in bundle.summary.get("joint_metrics") or []
            ), "data_quality": "PARTIAL" if partial else "VALID", "clock_quality": "VALID",
        }, f"{producer}:finish")
        self.outbox.update(
            producer, state="approved", remote_state="INGESTED",
            review_reason="ingestion accepted; no human review performed",
            next_attempt_at=0, last_error="",
        )
