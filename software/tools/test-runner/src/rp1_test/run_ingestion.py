"""Convert one finalized local run into the platform's exact ingestion schemas."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from uuid import UUID

import numpy as np
import yaml

from .api import IngestionClient
from .outbox import SQLiteOutbox
from .telemetry_spool import TelemetrySpool
from .trajectory import sha256_file
from .units import rad_to_deg

MAX_SERIES_PER_BATCH = 20
MAX_POINTS_PER_SERIES = 5000
MAX_EVENTS_PER_BATCH = 1000


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _stable_key(operation: str, payload: dict[str, Any]) -> str:
    digest = hashlib.sha256(_canonical_json(payload)).hexdigest()[:32]
    return f"rp1-{operation}-{digest}"[:200]


def _json_value(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    return value


def _parse_datetime(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _uuid(value: Any, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"platform.execution_context.{label} must be a UUID") from exc


def _parse_csv_value(name: str, value: str) -> Any:
    if value == "":
        return None
    if name in {"motor_index", "error_id", "sample"}:
        parsed = float(value)
        return int(parsed) if math.isfinite(parsed) else None
    if name.endswith(("_s", "_rad", "_rad_s", "_nm", "_c", "_v", "_a")):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    return value


def _read_telemetry(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return [
            {name: _parse_csv_value(name, value) for name, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _iter_events(path: Path) -> Iterator[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} event must be a JSON object")
            yield _json_value(value)


def _read_events(path: Path) -> list[dict[str, Any]]:
    return list(_iter_events(path))


def _verify_manifest(run_dir: Path, manifest: dict[str, Any]) -> dict[str, str]:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest files must be a list")
    verified = {}
    for item in files:
        path = run_dir / str(item["name"])
        if not path.is_file():
            raise FileNotFoundError(f"manifest file is missing: {path}")
        if path.stat().st_size != int(item["size_bytes"]):
            raise ValueError(f"manifest size mismatch: {path.name}")
        digest = sha256_file(path)
        if digest != str(item["sha256"]):
            raise ValueError(f"manifest SHA-256 mismatch: {path.name}")
        verified[str(item["name"])] = digest
    return verified


def _execution_payload(
    config: dict[str, Any],
    metadata: dict[str, Any],
    producer_execution_key: str,
) -> dict[str, Any]:
    platform = config.get("platform")
    context = platform.get("execution_context") if isinstance(platform, dict) else None
    if not isinstance(context, dict):
        raise ValueError("platform.execution_context must be configured")
    required_ids = (
        "campaign_id",
        "cycle_id",
        "segment_id",
        "asset_id",
        "configuration_id",
        "test_case_version_id",
        "station_id",
    )
    result = {
        "producer_key": producer_execution_key,
        **{name: _uuid(context.get(name), name) for name in required_ids},
    }
    configured_started = context.get("started_at")
    if configured_started == "$RUN_STARTED_AT":
        started_at = _parse_datetime(metadata.get("started_at"), "metadata.started_at")
    else:
        started_at = _parse_datetime(configured_started, "platform.execution_context.started_at")
    if context.get("source_status", "running") != "running":
        raise ValueError("platform.execution_context.source_status must be running")
    stage_code = str(context.get("stage_code", "")).strip()
    stage_name = str(context.get("stage_name", "")).strip()
    source_metadata = context.get("source_metadata", {})
    if not stage_code or len(stage_code) > 100:
        raise ValueError("platform.execution_context.stage_code is invalid")
    if not stage_name or len(stage_name) > 255:
        raise ValueError("platform.execution_context.stage_name is invalid")
    if not isinstance(source_metadata, dict):
        raise ValueError("platform.execution_context.source_metadata must be an object")
    result.update(
        {
            "started_at": _timestamp(started_at),
            "source_status": "running",
            "stage_code": stage_code,
            "stage_name": stage_name,
            "source_metadata": {
                **_json_value(source_metadata),
                "run_id": metadata.get("run_id"),
                "trajectory_sha256": metadata.get("trajectory_sha256"),
                "source_kind": metadata.get(
                    "source_kind", source_metadata.get("source_kind", "real")
                ),
            },
        }
    )
    return result


def _convert_events(
    raw_events: list[dict[str, Any]],
    metadata: dict[str, Any],
    started_at: datetime,
    *,
    start_index: int = 0,
) -> list[dict[str, Any]]:
    if not raw_events:
        raw_events = [
            {
                "type": "RUN_FINALIZED",
                "occurred_at": metadata.get("ended_at"),
                "status": metadata.get("status"),
            }
        ]
    converted = []
    for index, raw in enumerate(raw_events, start_index):
        absolute = raw.get("occurred_at") or raw.get("recorded_at")
        clock_quality = "VALID"
        try:
            occurred_at = _parse_datetime(absolute, "event.occurred_at")
        except ValueError:
            elapsed = float(raw.get("elapsed_s", 0.0))
            if elapsed < 0 or not math.isfinite(elapsed):
                raise ValueError("event elapsed_s must be finite and non-negative")
            occurred_at = started_at + timedelta(seconds=elapsed)
            clock_quality = "PARTIAL"
        event_type = str(raw.get("event_type") or raw.get("type") or "").strip()
        if not event_type or len(event_type) > 100:
            raise ValueError("event_type must contain 1-100 characters")
        payload = {
            key: value
            for key, value in raw.items()
            if key
            not in {
                "occurred_at",
                "recorded_at",
                "event_type",
                "type",
                "clock_quality",
                "data_quality",
            }
        }
        identity = {
            "index": index,
            "event_type": event_type,
            "occurred_at": _timestamp(occurred_at),
            "payload": payload,
        }
        converted.append(
            {
                "producer_event_key": _stable_key("event", identity),
                "event_type": event_type,
                "occurred_at": _timestamp(occurred_at),
                "payload": _json_value(payload),
                "clock_quality": clock_quality,
                "data_quality": str(raw.get("data_quality", "VALID")),
            }
        )
        if converted[-1]["data_quality"] not in {
            "VALID",
            "PARTIAL",
            "INVALID",
            "MISSING",
        }:
            raise ValueError("event data_quality is invalid")
    return converted


def _effective_stride(rows: list[dict[str, Any]], requested_stride: int) -> int:
    times = np.asarray(
        [float(row["time_s"]) for row in rows if row.get("time_s") is not None],
        dtype=np.float64,
    )
    positive = np.diff(times)
    positive = positive[positive > 0]
    if positive.size == 0:
        return max(1, requested_stride)
    source_interval = float(np.median(positive))
    minimum_stride = max(1, int(math.ceil(0.010 / source_interval)))
    return max(requested_stride, minimum_stride)


def _series_for_metric(
    *,
    producer_execution_key: str,
    subject_code: str,
    motor_index: int,
    rows: list[dict[str, Any]],
    source_column: str,
    metric_code: str,
    metric_version: str,
    canonical_unit: str,
    started_at: datetime,
    telemetry_path: Path,
    source_stride: int,
    convert_radians: bool,
    source_sha256: str | None = None,
    chunk_start: int = 0,
) -> list[dict[str, Any]]:
    points_source = [row for row in rows if isinstance(row.get(source_column), (int, float))]
    if source_column in {"cmd_pos_rad", "pos_rad"} and not points_source:
        raise ValueError(f"motor_index={motor_index} has no finite {source_column} telemetry")
    if not points_source:
        return []
    source_sha256 = source_sha256 or sha256_file(telemetry_path)
    series_chunks = []
    for chunk_index, offset in enumerate(
        range(0, len(points_source), MAX_POINTS_PER_SERIES), chunk_start
    ):
        chunk = points_source[offset : offset + MAX_POINTS_PER_SERIES]
        observed = [started_at + timedelta(seconds=float(row["time_s"])) for row in chunk]
        values = [
            (
                float(rad_to_deg(float(row[source_column])))
                if convert_radians
                else float(row[source_column])
            )
            for row in chunk
        ]
        point_interval = (
            float(np.median(np.diff([item.timestamp() for item in observed]))) * 1000
            if len(observed) > 1
            else 10.0
        )
        point_interval = max(10.0, point_interval)
        identity = {
            "execution": producer_execution_key,
            "subject": subject_code,
            "metric": metric_code,
            "chunk": chunk_index,
            "source_sha256": source_sha256,
        }
        series_chunks.append(
            {
                "producer_series_key": _stable_key("series", identity),
                "metric_code": metric_code,
                "metric_version": metric_version,
                "subject_code": subject_code,
                "canonical_unit": canonical_unit,
                "series_kind": "TRANSIENT",
                "started_at": _timestamp(observed[0]),
                "ended_at": _timestamp(observed[-1]),
                "sampling_interval_ms": point_interval,
                "downsample_method": "UNIFORM",
                "quality_status": "VALID",
                "raw_data_reference": {
                    "file_name": telemetry_path.name,
                    "sha256": source_sha256,
                    "column": source_column,
                    "motor_index": motor_index,
                    "source_stride": source_stride,
                },
                "points": [
                    {
                        "sample_index": index,
                        "observed_at": _timestamp(timestamp),
                        "value": value,
                        "quality_status": "VALID",
                    }
                    for index, (timestamp, value) in enumerate(zip(observed, values))
                ],
            }
        )
    return series_chunks


def _telemetry_series(
    rows: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    *,
    producer_execution_key: str,
    started_at: datetime,
    telemetry_path: Path,
    requested_stride: int,
    metric_version: str,
) -> list[dict[str, Any]]:
    all_series: list[dict[str, Any]] = []
    source_sha256 = sha256_file(telemetry_path)
    for mapping in sorted(mappings, key=lambda item: int(item["motor_index"])):
        motor_index = int(mapping["motor_index"])
        joint_rows = sorted(
            [row for row in rows if row.get("motor_index") == motor_index],
            key=lambda row: float(row["time_s"]),
        )
        if not joint_rows:
            raise ValueError(f"telemetry has no rows for motor_index={motor_index}")
        stride = _effective_stride(joint_rows, requested_stride)
        sampled = []
        for row in joint_rows[::stride]:
            actual = row.get("pos_rad")
            target = row.get("cmd_pos_rad")
            error = None
            if (
                isinstance(actual, (int, float))
                and isinstance(target, (int, float))
                and math.isfinite(actual)
                and math.isfinite(target)
            ):
                difference = actual - target
                if math.isfinite(difference):
                    error = difference
            sampled.append({**row, "tracking_error_rad": error})
        common = {
            "producer_execution_key": producer_execution_key,
            "subject_code": str(mapping["telemetry_subject_code"]),
            "motor_index": motor_index,
            "rows": sampled,
            "metric_version": metric_version,
            "started_at": started_at,
            "telemetry_path": telemetry_path,
            "source_stride": stride,
            "source_sha256": source_sha256,
        }
        all_series.extend(
            _series_for_metric(
                **common,
                source_column="cmd_pos_rad",
                metric_code="JOINT-TARGET-POSITION",
                canonical_unit="deg",
                convert_radians=True,
            )
        )
        all_series.extend(
            _series_for_metric(
                **common,
                source_column="pos_rad",
                metric_code="JOINT-ACTUAL-POSITION",
                canonical_unit="deg",
                convert_radians=True,
            )
        )
        error_series = _series_for_metric(
            **common,
            source_column="tracking_error_rad",
            metric_code="JOINT-TRACKING-ERROR",
            canonical_unit="deg",
            convert_radians=True,
        )
        for series in error_series:
            reference = series["raw_data_reference"]
            reference.pop("column")
            reference.update(
                derived=True,
                expression="pos_rad - cmd_pos_rad",
                source_columns=["pos_rad", "cmd_pos_rad"],
                source_unit="rad",
            )
        all_series.extend(error_series)
        all_series.extend(
            _series_for_metric(
                **common,
                source_column="spd_rad_s",
                metric_code="JOINT-VELOCITY",
                canonical_unit="deg/s",
                convert_radians=True,
            )
        )
        all_series.extend(
            _series_for_metric(
                **common,
                source_column="torque_nm",
                metric_code="JOINT-TORQUE",
                canonical_unit="N·m",
                convert_radians=False,
            )
        )
        all_series.extend(
            _series_for_metric(
                **common,
                source_column="temp_c",
                metric_code="JOINT-TEMPERATURE",
                canonical_unit="°C",
                convert_radians=False,
            )
        )
    return all_series


def _iter_spooled_series(
    spool: TelemetrySpool,
    mappings: list[dict[str, Any]],
    *,
    producer_execution_key: str,
    started_at: datetime,
    telemetry_path: Path,
    requested_stride: int,
    metric_version: str,
    source_sha256: str,
) -> Iterator[dict[str, Any]]:
    metrics = (
        ("cmd_pos_rad", "JOINT-TARGET-POSITION", "deg", True),
        ("pos_rad", "JOINT-ACTUAL-POSITION", "deg", True),
        ("tracking_error_rad", "JOINT-TRACKING-ERROR", "deg", True),
        ("spd_rad_s", "JOINT-VELOCITY", "deg/s", True),
        ("torque_nm", "JOINT-TORQUE", "N·m", False),
        ("temp_c", "JOINT-TEMPERATURE", "°C", False),
    )
    for mapping in sorted(mappings, key=lambda item: int(item["motor_index"])):
        motor_index = int(mapping["motor_index"])
        stride = spool.sample_motor(motor_index, requested_stride)
        for column, metric, unit, convert in metrics:
            found = False
            for chunk_index, rows in enumerate(spool.metric_chunks(column, MAX_POINTS_PER_SERIES)):
                found = True
                series = _series_for_metric(
                    producer_execution_key=producer_execution_key,
                    subject_code=str(mapping["telemetry_subject_code"]),
                    motor_index=motor_index,
                    rows=rows,
                    source_column=column,
                    metric_code=metric,
                    metric_version=metric_version,
                    canonical_unit=unit,
                    started_at=started_at,
                    telemetry_path=telemetry_path,
                    source_stride=stride,
                    convert_radians=convert,
                    source_sha256=source_sha256,
                    chunk_start=chunk_index,
                )[0]
                if column == "tracking_error_rad":
                    reference = series["raw_data_reference"]
                    reference.pop("column")
                    reference.update(
                        derived=True,
                        expression="pos_rad - cmd_pos_rad",
                        source_columns=["pos_rad", "cmd_pos_rad"],
                        source_unit="rad",
                    )
                yield series
            if not found and column in {"cmd_pos_rad", "pos_rad"}:
                raise ValueError(f"motor_index={motor_index} has no finite {column} telemetry")


def _event_batches(
    path: Path, metadata: dict[str, Any], started_at: datetime
) -> Iterator[list[dict[str, Any]]]:
    batch = []
    count = 0
    for count, raw in enumerate(_iter_events(path), 1):
        batch.extend(_convert_events([raw], metadata, started_at, start_index=count - 1))
        if len(batch) == MAX_EVENTS_PER_BATCH:
            yield batch
            batch = []
    if not count:
        batch = _convert_events([], metadata, started_at)
    if batch:
        yield batch


class _StagingOutbox(SQLiteOutbox):
    """Use the existing queue schema while closing temporary DB connections."""

    @contextmanager
    def _connect(self):
        with closing(super()._connect()) as db, db:
            db.execute("PRAGMA cache_size=-2048")
            yield db


def _publish_staged(staged: SQLiteOutbox, outbox: SQLiteOutbox) -> None:
    # One transaction publishes the complete run, including finish. Invalid
    # input or any insert failure leaves the destination queue unchanged. The
    # SQL copy avoids loading all serialized requests back into Python memory.
    with closing(outbox._connect()) as db:
        db.execute("ATTACH DATABASE ? AS staged_run", (str(staged.path),))
        with db:
            db.execute(
                """
                INSERT OR IGNORE INTO outbox (
                    idempotency_key, method, path, payload_json, created_at,
                    producer_execution_key, requires_stage_id, captures_resolution
                )
                SELECT idempotency_key, method, path, payload_json, created_at,
                       producer_execution_key, requires_stage_id, captures_resolution
                FROM staged_run.outbox ORDER BY id
                """
            )


def _artifact_payloads(
    run_dir: Path,
    manifest: dict[str, Any],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    kind_by_suffix = {
        ".csv": "TELEMETRY_CSV",
        ".jsonl": "EVENT_LOG",
        ".log": "NATIVE_LOG",
        ".yaml": "CONFIG_SNAPSHOT",
        ".yml": "CONFIG_SNAPSHOT",
        ".json": "RUN_METADATA",
    }
    items = list(manifest["files"])
    manifest_path = run_dir / "manifest.json"
    items.append(
        {
            "name": manifest_path.name,
            "size_bytes": manifest_path.stat().st_size,
            "sha256": sha256_file(manifest_path),
        }
    )
    payloads = []
    for item in items:
        file_name = str(item["name"])
        suffix = Path(file_name).suffix.lower()
        payloads.append(
            {
                "producer_key": _stable_key("artifact", item),
                "artifact_kind": (
                    "RUN_MANIFEST" if file_name == "manifest.json" else kind_by_suffix[suffix]
                ),
                "availability_status": "MISSING",
                "object_key": None,
                "source_location": f"edge-local:{run_dir.name}/{file_name}",
                "file_name": file_name,
                "mime_type": {
                    ".csv": "text/csv",
                    ".jsonl": "application/x-ndjson",
                    ".json": "application/json",
                    ".yaml": "application/yaml",
                    ".yml": "application/yaml",
                    ".log": "text/plain",
                }[suffix],
                "size_bytes": int(item["size_bytes"]),
                "sha256": str(item["sha256"]),
                "metadata": {"local_only": True, "run_id": run_dir.name},
                "coordinate_frame": "",
            }
        )
    trajectory_sha = metadata.get("trajectory_sha256")
    trajectory_name = metadata.get("trajectory_file_name")
    if trajectory_sha and trajectory_name:
        payloads.append(
            {
                "producer_key": _stable_key(
                    "trajectory-artifact",
                    {"name": trajectory_name, "sha256": trajectory_sha},
                ),
                "artifact_kind": "TRAJECTORY_NPZ",
                "availability_status": "MISSING",
                "object_key": None,
                "source_location": f"edge-input:{trajectory_name}",
                "file_name": str(trajectory_name),
                "mime_type": "application/octet-stream",
                "size_bytes": metadata.get("trajectory_size_bytes"),
                "sha256": str(trajectory_sha),
                "metadata": {"local_only": True, "run_id": run_dir.name},
                "trajectory_key": str(
                    metadata.get("trajectory_key") or Path(str(trajectory_name)).stem
                ),
                "trajectory_version": int(metadata.get("trajectory_version", 1)),
                "coordinate_frame": str(metadata.get("coordinate_frame", "motor")),
                "trajectory_point_count": metadata.get("trajectory_point_count"),
            }
        )
    return payloads


def _finish_payload(
    metadata: dict[str, Any],
    telemetry_rows: list[dict[str, Any]],
    execution_started_at: datetime,
) -> dict[str, Any]:
    finished = _parse_datetime(metadata.get("ended_at"), "metadata.ended_at")
    if finished < execution_started_at:
        raise ValueError("metadata.ended_at must not precede execution started_at")
    wall_seconds = max(0.0, (finished - execution_started_at).total_seconds())
    del telemetry_rows  # Samples alone do not confirm a task was active.
    # Telemetry can include homing, cooldown and operator pauses. Only a
    # confirmed counter from the runner is eligible as active time.
    requested_active = float(metadata.get("active_seconds", 0.0))
    if not math.isfinite(requested_active) or requested_active < 0:
        raise ValueError("metadata.active_seconds must be finite and non-negative")
    active_seconds = min(max(0.0, requested_active), wall_seconds)
    local_status = str(metadata.get("status", "")).lower()
    if local_status == "passed" and metadata.get("source_kind") != "synthetic":
        source_status, outcome, termination = "passed", "PASSED", "NORMAL"
    elif local_status == "failed":
        source_status, outcome, termination = "failed", "FAILED", "SYSTEM_CRASH"
    else:
        source_status, outcome, termination = (
            "blocked",
            "INCONCLUSIVE",
            "UNKNOWN",
        )
    configured_termination = metadata.get("termination_kind")
    if configured_termination is not None:
        termination = str(configured_termination)
    if termination not in {
        "NORMAL",
        "MANUAL_STOP",
        "SAFETY_WATCHDOG",
        "DATA_TIMEOUT",
        "SYSTEM_CRASH",
        "UNKNOWN",
    }:
        raise ValueError("metadata.termination_kind is invalid")
    data_quality = str(metadata.get("data_quality", "VALID"))
    clock_quality = str(metadata.get("clock_quality", "VALID"))
    if data_quality not in {"VALID", "PARTIAL", "INVALID", "MISSING"}:
        raise ValueError("metadata.data_quality is invalid")
    if clock_quality not in {"VALID", "PARTIAL", "INVALID"}:
        raise ValueError("metadata.clock_quality is invalid")
    return {
        "producer_key": _stable_key(
            "finish", {"run_id": metadata.get("run_id"), "finished_at": _timestamp(finished)}
        ),
        "source_status": source_status,
        "finished_at": _timestamp(finished),
        "active_seconds": active_seconds,
        "outcome": outcome,
        "termination_kind": termination,
        "summary": str(metadata.get("summary", ""))[:5000],
        "issues": str(metadata.get("issues", ""))[:10000],
        "exception_count": int(metadata.get("exception_count", 0)),
        "data_quality": data_quality,
        "clock_quality": clock_quality,
    }


def enqueue_run(
    run_dir: Path,
    outbox: SQLiteOutbox,
    *,
    producer_execution_key: str | None = None,
    telemetry_stride: int = 10,
) -> dict[str, Any]:
    run_dir = run_dir.expanduser().resolve()
    manifest_path = run_dir / "manifest.json"
    metadata_path = run_dir / "metadata.json"
    telemetry_path = run_dir / "telemetry.csv"
    events_path = run_dir / "events.jsonl"
    for path in (manifest_path, metadata_path, telemetry_path, events_path):
        if not path.is_file():
            raise FileNotFoundError(f"required run artifact not found: {path}")
    if telemetry_stride < 1:
        raise ValueError("telemetry stride must be positive")
    snapshot = next(iter(sorted(run_dir.glob("config.snapshot.*"))), None)
    if snapshot is None:
        raise FileNotFoundError("run has no config snapshot")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    config = yaml.safe_load(snapshot.read_text(encoding="utf-8")) or {}
    if not all(isinstance(value, dict) for value in (manifest, metadata, config)):
        raise ValueError("manifest, metadata, and config snapshot must be objects")
    verified_hashes = _verify_manifest(run_dir, manifest)
    producer_key = IngestionClient.validate_producer_execution_key(
        str(producer_execution_key or metadata.get("run_id") or manifest.get("run_id") or "")
    )
    mappings = config.get("joint_mappings")
    if not isinstance(mappings, list) or not mappings:
        raise ValueError("config snapshot has no joint_mappings")
    platform = config.get("platform", {})
    metric_version = str(platform.get("metric_version", "1.0.0"))
    if not metric_version or len(metric_version) > 50:
        raise ValueError("platform.metric_version is invalid")
    create_payload = _execution_payload(config, metadata, producer_key)
    started_at = _parse_datetime(create_payload["started_at"], "create.started_at")
    artifact_payloads = _artifact_payloads(run_dir, manifest, metadata)
    finish_payload = _finish_payload(metadata, [], started_at)
    telemetry_sha256 = verified_hashes.get("telemetry.csv")
    if telemetry_sha256 is None:
        telemetry_sha256 = sha256_file(telemetry_path)

    # Sorting, sampling and request staging live on disk. Only one telemetry
    # batch (20 series of at most 5000 points) or 1000 events is materialized.
    with TemporaryDirectory(prefix="rp1-ingestion-") as temporary:
        temporary_path = Path(temporary)
        staged = _StagingOutbox(temporary_path / "requests.sqlite3")
        client = IngestionClient("", "", staged)
        queued_keys = [
            client.create_execution(create_payload, _stable_key("create-request", create_payload))
        ]
        event_count = 0
        for batch_index, event_items in enumerate(
            _event_batches(events_path, metadata, started_at)
        ):
            payload = {
                "producer_key": _stable_key(f"events-{batch_index}", {"events": event_items}),
                "events": event_items,
            }
            queued_keys.append(
                client.events(
                    producer_key, payload, _stable_key(f"events-request-{batch_index}", payload)
                )
            )
            event_count += len(event_items)
        # Release the final event batch before building telemetry payloads.
        del event_items, payload

        telemetry_batches = telemetry_series_count = 0
        with closing(TelemetrySpool(temporary_path / "telemetry.sqlite3")) as spool:
            spool.load(telemetry_path, _parse_csv_value)
            telemetry_row_count = spool.row_count
            series_batch = []
            for series in _iter_spooled_series(
                spool,
                mappings,
                producer_execution_key=producer_key,
                started_at=started_at,
                telemetry_path=telemetry_path,
                requested_stride=telemetry_stride,
                metric_version=metric_version,
                source_sha256=telemetry_sha256,
            ):
                series_batch.append(series)
                telemetry_series_count += 1
                if len(series_batch) == MAX_SERIES_PER_BATCH:
                    payload = {
                        "producer_key": _stable_key(
                            f"telemetry-{telemetry_batches}", {"series": series_batch}
                        ),
                        "series": series_batch,
                    }
                    queued_keys.append(
                        client.telemetry(
                            producer_key,
                            payload,
                            _stable_key(f"telemetry-request-{telemetry_batches}", payload),
                        )
                    )
                    telemetry_batches += 1
                    series_batch = []
                    del payload
            if series_batch:
                payload = {
                    "producer_key": _stable_key(
                        f"telemetry-{telemetry_batches}", {"series": series_batch}
                    ),
                    "series": series_batch,
                }
                queued_keys.append(
                    client.telemetry(
                        producer_key,
                        payload,
                        _stable_key(f"telemetry-request-{telemetry_batches}", payload),
                    )
                )
                telemetry_batches += 1
            if not telemetry_series_count:
                raise ValueError("telemetry conversion produced no series")
        for index, payload in enumerate(artifact_payloads):
            queued_keys.append(
                client.artifact(
                    producer_key, payload, _stable_key(f"artifact-request-{index}", payload)
                )
            )
        queued_keys.append(
            client.finish_execution(
                producer_key, finish_payload, _stable_key("finish-request", finish_payload)
            )
        )
        _publish_staged(staged, outbox)
    return {
        "producer_execution_key": producer_key,
        "queued_requests": len(queued_keys),
        "telemetry_rows": telemetry_row_count,
        "telemetry_series": telemetry_series_count,
        "telemetry_batches": telemetry_batches,
        "artifact_requests": len(artifact_payloads),
        "event_count": event_count,
        "idempotency_keys": queued_keys,
    }
