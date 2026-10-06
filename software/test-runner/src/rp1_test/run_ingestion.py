"""Convert one finalized local run into the platform's exact ingestion schemas."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import yaml

from .api import IngestionClient
from .outbox import SQLiteOutbox
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


def _read_events(path: Path) -> list[dict[str, Any]]:
    events = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} event must be a JSON object")
        events.append(_json_value(value))
    return events


def _verify_manifest(run_dir: Path, manifest: dict[str, Any]) -> None:
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest files must be a list")
    for item in files:
        path = run_dir / str(item["name"])
        if not path.is_file():
            raise FileNotFoundError(f"manifest file is missing: {path}")
        if path.stat().st_size != int(item["size_bytes"]):
            raise ValueError(f"manifest size mismatch: {path.name}")
        if sha256_file(path) != str(item["sha256"]):
            raise ValueError(f"manifest SHA-256 mismatch: {path.name}")


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
    for index, raw in enumerate(raw_events):
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
) -> list[dict[str, Any]]:
    points_source = [row for row in rows if isinstance(row.get(source_column), (int, float))]
    if source_column in {"cmd_pos_rad", "pos_rad"} and not points_source:
        raise ValueError(f"motor_index={motor_index} has no finite {source_column} telemetry")
    if not points_source:
        return []
    series_chunks = []
    for chunk_index, offset in enumerate(range(0, len(points_source), MAX_POINTS_PER_SERIES)):
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
            "source_sha256": sha256_file(telemetry_path),
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
                    "sha256": sha256_file(telemetry_path),
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
    _verify_manifest(run_dir, manifest)
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
    telemetry_rows = _read_telemetry(telemetry_path)
    series = _telemetry_series(
        telemetry_rows,
        mappings,
        producer_execution_key=producer_key,
        started_at=started_at,
        telemetry_path=telemetry_path,
        requested_stride=telemetry_stride,
        metric_version=metric_version,
    )
    if not series:
        raise ValueError("telemetry conversion produced no series")

    client = IngestionClient("", "", outbox)
    queued_keys = [
        client.create_execution(create_payload, _stable_key("create-request", create_payload))
    ]
    converted_events = _convert_events(_read_events(events_path), metadata, started_at)
    for batch_index, offset in enumerate(range(0, len(converted_events), MAX_EVENTS_PER_BATCH)):
        event_items = converted_events[offset : offset + MAX_EVENTS_PER_BATCH]
        payload = {
            "producer_key": _stable_key(f"events-{batch_index}", {"events": event_items}),
            "events": event_items,
        }
        queued_keys.append(
            client.events(
                producer_key,
                payload,
                _stable_key(f"events-request-{batch_index}", payload),
            )
        )
    telemetry_batches = 0
    for batch_index, offset in enumerate(range(0, len(series), MAX_SERIES_PER_BATCH)):
        series_batch = series[offset : offset + MAX_SERIES_PER_BATCH]
        payload = {
            "producer_key": _stable_key(f"telemetry-{batch_index}", {"series": series_batch}),
            "series": series_batch,
        }
        queued_keys.append(
            client.telemetry(
                producer_key,
                payload,
                _stable_key(f"telemetry-request-{batch_index}", payload),
            )
        )
        telemetry_batches += 1
    artifact_payloads = _artifact_payloads(run_dir, manifest, metadata)
    for index, payload in enumerate(artifact_payloads):
        queued_keys.append(
            client.artifact(
                producer_key,
                payload,
                _stable_key(f"artifact-request-{index}", payload),
            )
        )
    finish_payload = _finish_payload(metadata, telemetry_rows, started_at)
    queued_keys.append(
        client.finish_execution(
            producer_key,
            finish_payload,
            _stable_key("finish-request", finish_payload),
        )
    )
    return {
        "producer_execution_key": producer_key,
        "queued_requests": len(queued_keys),
        "telemetry_rows": len(telemetry_rows),
        "telemetry_series": len(series),
        "telemetry_batches": telemetry_batches,
        "artifact_requests": len(artifact_payloads),
        "event_count": len(converted_events),
        "idempotency_keys": queued_keys,
    }
