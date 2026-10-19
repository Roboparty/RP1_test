"""Generate a bounded, accelerated synthetic recording for software integration.

This is not a robot simulator or a reliability test result. All timestamps share
one explicitly simulated UTC clock. No vendor adapter is imported.
"""

from __future__ import annotations

import math
import tempfile
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml
from rp1_test.api import IngestionClient
from rp1_test.artifacts import RunArtifacts
from rp1_test.commands.common import load_config
from rp1_test.run_ingestion import _execution_payload

CONTEXT_IDS = (
    "campaign_id",
    "cycle_id",
    "segment_id",
    "asset_id",
    "configuration_id",
    "test_case_version_id",
    "station_id",
)


def execution_context(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy only public context fields; never put an API key in a run artifact."""
    payload = raw.get("execution_context", raw)
    if not isinstance(payload, dict):
        raise ValueError("context.execution_context must be an object")
    source_metadata = payload.get("source_metadata", {})
    if not isinstance(source_metadata, dict):
        raise ValueError("context.source_metadata must be an object")
    return {
        **{key: payload.get(key) for key in CONTEXT_IDS},
        "started_at": "$RUN_STARTED_AT",
        "source_status": "running",
        "stage_code": "DEMO-SYNTHETIC",
        "stage_name": "Synthetic software integration demonstration",
        "source_metadata": {
            # Select explicitly; arbitrary keys might contain operator credentials.
            "collector": "rp1-demo",
            "source_kind": "synthetic",
            "clock_kind": "simulated",
        },
    }


def intervals(duration: float, pauses: list[tuple[float, float]]) -> list[dict[str, float]]:
    """Confirm the complement of non-overlapping scheduled pauses."""
    result = []
    cursor = 0.0
    for start, length in sorted(pauses):
        if not all(math.isfinite(value) for value in (start, length)):
            raise ValueError("pause values must be finite")
        end = start + length
        if start < 0 or length <= 0 or end > duration or start < cursor:
            raise ValueError("pauses must be positive, non-overlapping and inside duration")
        if start > cursor:
            result.append({"start_s": cursor, "end_s": start})
        cursor = end
    if cursor < duration:
        result.append({"start_s": cursor, "end_s": duration})
    return result


def generate_demo(
    config_path: Path,
    output_dir: Path,
    run_id: str,
    *,
    duration: float = 30.0,
    sample_rate: float = 20.0,
    pauses: list[tuple[float, float]] | None = None,
    context: dict[str, Any] | None = None,
) -> Path:
    if not math.isfinite(duration) or duration <= 0 or duration > 3600:
        raise ValueError("duration must be finite and in (0, 3600] seconds")
    if not math.isfinite(sample_rate) or sample_rate < 1 or sample_rate > 100:
        raise ValueError("sample-rate must be finite and in [1, 100] Hz")
    IngestionClient.validate_producer_execution_key(run_id)
    if Path(run_id).name != run_id or run_id in {".", ".."}:
        raise ValueError("run-id must be a single directory name")
    active_intervals = intervals(duration, pauses or [])
    config = deepcopy(load_config(config_path))
    mappings = config["joint_mappings"]
    if len(mappings) != 2 or config.get("limb") != "DEMO":
        raise ValueError("demo config must declare limb DEMO and exactly two joint mappings")
    started = datetime.now(timezone.utc) - timedelta(seconds=duration)
    platform = config.setdefault("platform", {})
    # A context from a hardware template must never leak into an offline demo.
    platform.pop("execution_context", None)
    if context is not None:
        platform["execution_context"] = execution_context(context)
        _execution_payload(config, {"started_at": started.isoformat()}, run_id)
    config["source_kind"] = "synthetic"
    config["demo"] = {
        "duration_s": duration,
        "sample_rate_hz": sample_rate,
        "pauses": [{"start_s": start, "duration_s": length} for start, length in pauses or []],
    }
    with tempfile.TemporaryDirectory(prefix="rp1-demo-") as staging:
        snapshot = Path(staging) / "config.yaml"
        snapshot.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        artifacts = RunArtifacts(output_dir, run_id, snapshot)
    artifacts.started_at = started.isoformat()
    artifacts.start_telemetry(
        [
            "time_s",
            "joint_name",
            "motor_index",
            "cmd_pos_rad",
            "pos_rad",
            "spd_rad_s",
            "torque_nm",
            "temp_c",
            "error_id",
            "phase",
            "task_time_s",
            "source_kind",
        ]
    )
    artifacts.native_log_path.write_text(
        "source_kind=synthetic; clock_kind=simulated; robot_connected=false\n", encoding="utf-8"
    )

    events: list[dict[str, Any]] = []

    def event(event_type: str, seconds: float, **extra: Any) -> None:
        events.append(
            {
                "type": event_type,
                "elapsed_s": seconds,
                "occurred_at": (started + timedelta(seconds=seconds)).isoformat(),
                "source_kind": "synthetic",
                **extra,
            }
        )

    event("DEMO_STARTED", 0.0)
    for start, length in sorted(pauses or []):
        event("PAUSED", start, reason="scheduled demonstration pause")
        event("RESUMED", start + length)
    for interval in active_intervals:
        event("ACTIVE_START", interval["start_s"])
        event("ACTIVE_END", interval["end_s"])
    event("DEMO_FINISHED", duration, test_outcome="INCONCLUSIVE")
    priority = {
        "DEMO_STARTED": 0,
        "ACTIVE_END": 1,
        "PAUSED": 2,
        "RESUMED": 3,
        "ACTIVE_START": 4,
        "DEMO_FINISHED": 5,
    }
    for item in sorted(events, key=lambda item: (item["elapsed_s"], priority[item["type"]])):
        artifacts.write_event(item)
    count = math.ceil(duration * sample_rate)
    for sample in range(count + 1):
        seconds = min(sample / sample_rate, duration)
        active = any(item["start_s"] <= seconds < item["end_s"] for item in active_intervals)
        task_time = sum(
            max(0.0, min(seconds, item["end_s"]) - item["start_s"]) for item in active_intervals
        )
        phase = "active" if active else ("finished" if seconds == duration else "paused")
        for index, mapping in enumerate(mappings):
            angle = 2 * math.pi * task_time / 8 + index * math.pi / 4
            target = 0.2 * math.sin(angle)
            artifacts.write_telemetry(
                {
                    "time_s": seconds,
                    "joint_name": mapping["joint_name"],
                    "motor_index": int(mapping["motor_index"]),
                    "cmd_pos_rad": target,
                    "pos_rad": target - (0.003 * math.sin(angle + 0.2) if active else 0.0),
                    "spd_rad_s": 0.2 * math.pi / 4 * math.cos(angle) if active else 0.0,
                    "torque_nm": 0.2 + 0.03 * math.sin(angle) if active else 0.1,
                    "temp_c": 27 + 3 * (1 - math.exp(-task_time / 40)) + index,
                    "error_id": 0,
                    "phase": phase,
                    "task_time_s": task_time,
                    "source_kind": "synthetic",
                }
            )
    active_seconds = sum(item["end_s"] - item["start_s"] for item in active_intervals)
    manifest = artifacts.finalize(
        status="synthetic",
        extra={
            "source_kind": "synthetic",
            "clock_kind": "simulated",
            "clock_quality": "PARTIAL",
            "data_quality": "PARTIAL",
            "ended_at": (started + timedelta(seconds=duration)).isoformat(),
            "elapsed_seconds": duration,
            "active_seconds": active_seconds,
            "confirmed_active_intervals": active_intervals,
            "active_time_scope": "scheduled demonstration active phases; excludes pauses",
            "test_outcome": "INCONCLUSIVE",
            "robot_connected": False,
            "termination_kind": "NORMAL",
            "exception_count": 0,
            "summary": "Synthetic integration demonstration; no robot reliability verdict.",
            "telemetry_gaps": config.get("telemetry_gaps", {}),
        },
    )
    return manifest.parent
