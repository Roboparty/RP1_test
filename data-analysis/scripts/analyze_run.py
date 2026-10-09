#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Summarize native rp1-test run files without services or third-party packages."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from bisect import bisect_right
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

VERSION = "0.1.0"


def finite(value: Any, label: str) -> float | None:
    if isinstance(value, bool):
        raise ValueError(f"{label}: a boolean is not a numeric measurement")
    if value is None or str(value).strip().lower() in {"", "nan", "null", "none"}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: expected a number or a missing value") from exc
    return number if math.isfinite(number) else None


def timestamp(value: Any) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return result


def confirmed_intervals(
    metadata: dict[str, Any], wall: float | None
) -> list[dict] | None:
    raw = metadata.get("confirmed_active_intervals")
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ValueError("confirmed_active_intervals must be a list")
    intervals = []
    previous_end = 0.0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError("each active interval must be an object")
        start = finite(item.get("start_s"), "interval.start_s")
        end = finite(item.get("end_s"), "interval.end_s")
        if start is None or end is None or start < 0 or end <= start:
            raise ValueError("active intervals must have finite 0 <= start_s < end_s")
        if index and start < previous_end:
            raise ValueError("active intervals must be ordered and must not overlap")
        if wall is not None and end > wall + 1e-6:
            raise ValueError("active interval extends beyond the declared run duration")
        intervals.append({"start_s": start, "end_s": end})
        previous_end = end
    return intervals


@dataclass
class JointStats:
    motor_index: int
    joint_name: str
    rows: int = 0
    accepted_rows: int = 0
    duplicate_rows: int = 0
    out_of_order_rows: int = 0
    first_s: float | None = None
    last_s: float | None = None
    active_samples: int = 0
    error_samples: int = 0
    missing_error_samples: int = 0
    error_scale: float = 0.0
    error_scaled_squares: float = 0.0
    error_peak: float | None = None
    temperature_samples: int = 0
    missing_temperature_samples: int = 0
    temperature_min: float | None = None
    temperature_peak: float | None = None
    gap_count: int = 0
    largest_gap_s: float | None = None

    def add(
        self, row: dict, seconds: float, active: bool, max_gap: float | None
    ) -> None:
        self.rows += 1
        if self.last_s is not None:
            if seconds == self.last_s:
                self.duplicate_rows += 1
                return
            if seconds < self.last_s:
                self.out_of_order_rows += 1
                return
            delta = seconds - self.last_s
            if max_gap is not None and delta > max_gap:
                self.gap_count += 1
                self.largest_gap_s = max(delta, self.largest_gap_s or 0.0)
        if self.first_s is None:
            self.first_s = seconds
        self.last_s = seconds
        self.accepted_rows += 1
        temperature = finite(row.get("temp_c"), "temp_c")
        if temperature is None:
            self.missing_temperature_samples += 1
        else:
            self.temperature_samples += 1
            self.temperature_min = (
                min(temperature, self.temperature_min)
                if self.temperature_min is not None
                else temperature
            )
            self.temperature_peak = (
                max(temperature, self.temperature_peak)
                if self.temperature_peak is not None
                else temperature
            )
        if active:
            self.active_samples += 1
            actual = finite(row.get("pos_rad"), "pos_rad")
            command = finite(row.get("cmd_pos_rad"), "cmd_pos_rad")
            error = (
                actual - command if actual is not None and command is not None else None
            )
            if error is None or not math.isfinite(error):
                self.missing_error_samples += 1
            else:
                self.error_samples += 1
                magnitude = abs(error)
                # Normalize before squaring so a finite RMS cannot overflow
                # merely because the unnormalized sum of squares would.
                if magnitude > self.error_scale:
                    self.error_scaled_squares = (
                        1.0
                        + self.error_scaled_squares
                        * (self.error_scale / magnitude) ** 2
                    )
                    self.error_scale = magnitude
                elif magnitude:
                    self.error_scaled_squares += (magnitude / self.error_scale) ** 2
                self.error_peak = max(magnitude, self.error_peak or 0.0)

    def result(
        self, intervals_known: bool, ambient: float | None, gap_checked: bool
    ) -> dict:
        return {
            "motor_index": self.motor_index,
            "joint_name": self.joint_name,
            "rows": self.rows,
            "accepted_rows": self.accepted_rows,
            "duplicate_rows": self.duplicate_rows,
            "out_of_order_rows": self.out_of_order_rows,
            "first_sample_s": self.first_s,
            "last_sample_s": self.last_s,
            "active_samples": self.active_samples if intervals_known else None,
            "tracking_error": {
                "unit": "rad",
                "sample_count": self.error_samples,
                "missing_pair_count": self.missing_error_samples
                if intervals_known
                else None,
                "rms": self.error_scale
                * math.sqrt(self.error_scaled_squares / self.error_samples)
                if self.error_samples
                else None,
                "absolute_peak": self.error_peak,
            },
            "temperature": {
                "unit": "degC",
                "sample_count": self.temperature_samples,
                "missing_count": self.missing_temperature_samples,
                "minimum": self.temperature_min,
                "peak": self.temperature_peak,
                "rise_above_ambient": self.temperature_peak - ambient
                if self.temperature_peak is not None and ambient is not None
                else None,
            },
            "sampling_gaps": {
                "count": self.gap_count if gap_checked else None,
                "largest_interval_s": self.largest_gap_s,
            },
        }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def analyze(run_dir: Path, *, max_gap_s: float | None = None) -> dict:
    if max_gap_s is not None and (not math.isfinite(max_gap_s) or max_gap_s <= 0):
        raise ValueError("max_gap_s must be finite and positive")
    metadata_path = run_dir / "metadata.json"
    telemetry_path = run_dir / "telemetry.csv"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("metadata.json must be an object")
    wall = finite(metadata.get("elapsed_seconds"), "elapsed_seconds")
    if wall is None and metadata.get("started_at") and metadata.get("ended_at"):
        wall = (
            timestamp(metadata["ended_at"]) - timestamp(metadata["started_at"])
        ).total_seconds()
    if wall is not None and wall < 0:
        raise ValueError("run duration must be non-negative")
    intervals = confirmed_intervals(metadata, wall)
    active_seconds = (
        sum(item["end_s"] - item["start_s"] for item in intervals)
        if intervals is not None
        else None
    )
    ambient = finite(metadata.get("ambient_temperature_c"), "ambient_temperature_c")
    warnings = []
    if intervals is None:
        warnings.append(
            "No confirmed active intervals: active time and active tracking error are unavailable."
        )
    if ambient is None:
        warnings.append("No ambient temperature: temperature rise is unavailable.")
    if max_gap_s is None:
        warnings.append("No --max-gap-s: sampling gap assessment is unavailable.")
    declared_active = finite(metadata.get("active_seconds"), "active_seconds")
    if (
        declared_active is not None
        and active_seconds is not None
        and not math.isclose(declared_active, active_seconds, abs_tol=1e-6)
    ):
        warnings.append(
            "metadata.active_seconds differs from the confirmed intervals; intervals were used."
        )
    source_kind = metadata.get("source_kind", "unknown")
    if not isinstance(source_kind, str) or source_kind not in {
        "real",
        "synthetic",
        "unknown",
    }:
        raise ValueError("source_kind must be real, synthetic or unknown")
    if source_kind == "unknown":
        warnings.append("Data origin is not declared.")
    joints: dict[int, JointStats] = {}
    interval_starts = [item["start_s"] for item in intervals or []]
    with telemetry_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"time_s", "motor_index", "cmd_pos_rad", "pos_rad"}.issubset(
            reader.fieldnames or []
        ):
            raise ValueError(
                "telemetry.csv requires time_s,motor_index,cmd_pos_rad,pos_rad"
            )
        if len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise ValueError("telemetry.csv contains duplicate column names")
        for line_number, row in enumerate(reader, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"telemetry.csv:{line_number}: column count mismatch")
            seconds = finite(row["time_s"], f"line {line_number} time_s")
            index = finite(row["motor_index"], f"line {line_number} motor_index")
            if (
                seconds is None
                or seconds < 0
                or index is None
                or index < 0
                or not index.is_integer()
            ):
                raise ValueError(
                    f"telemetry.csv:{line_number}: invalid time_s or motor_index"
                )
            if wall is not None and seconds > wall + 1e-6:
                raise ValueError(
                    f"telemetry.csv:{line_number}: timestamp exceeds run duration"
                )
            motor_index = int(index)
            name = row.get("joint_name") or f"motor_{motor_index}"
            stats = joints.setdefault(motor_index, JointStats(motor_index, name))
            if stats.joint_name != name:
                raise ValueError(
                    f"motor_index {motor_index} has inconsistent joint names"
                )
            interval_index = bisect_right(interval_starts, seconds) - 1
            active = (
                interval_index >= 0 and seconds < intervals[interval_index]["end_s"]
            )
            stats.add(row, seconds, active, max_gap_s)
    if not joints:
        raise ValueError("telemetry.csv contains no samples")
    events_path = run_dir / "events.jsonl"
    events = []
    counts: Counter[str] = Counter()
    if events_path.exists():
        with events_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError(
                        f"events.jsonl:{line_number}: event must be an object"
                    )
                name = str(event.get("type") or event.get("event_type") or "UNKNOWN")
                counts[name] += 1
                if len(events) < 1000:
                    events.append(
                        {
                            "type": name,
                            "elapsed_s": finite(
                                event.get("elapsed_s"), "event.elapsed_s"
                            ),
                            "occurred_at": event.get("occurred_at")
                            or event.get("recorded_at"),
                        }
                    )
    else:
        warnings.append("events.jsonl is missing; event counts are unavailable.")
    if sum(counts.values()) > len(events):
        warnings.append(
            "Event preview is limited to the first 1000 records; counts include all events."
        )
    results = [
        joints[key].result(intervals is not None, ambient, max_gap_s is not None)
        for key in sorted(joints)
    ]
    if any(item["duplicate_rows"] or item["out_of_order_rows"] for item in results):
        warnings.append(
            "Duplicate and out-of-order rows were excluded from metrics; see per-joint counts."
        )
    inputs = [metadata_path, telemetry_path] + (
        [events_path] if events_path.exists() else []
    )
    return {
        "schema_version": "rp1-analysis/v1",
        "tool_version": VERSION,
        "run_id": str(metadata.get("run_id", run_dir.name)),
        "source_kind": source_kind,
        "runner_status": metadata.get("status"),
        "outcome": "INCONCLUSIVE",
        "wall_seconds": wall,
        "active_seconds": active_seconds,
        "declared_active_seconds": declared_active,
        "confirmed_active_intervals": intervals,
        "analysis_options": {
            "max_gap_s": max_gap_s,
            "error_definition": "pos_rad - cmd_pos_rad; no angle wrapping",
            "interval_end": "exclusive",
        },
        "inputs": [{"name": path.name, "sha256": sha256(path)} for path in inputs],
        "joints": results,
        "event_counts": dict(sorted(counts.items())),
        "events": events,
        "warnings": warnings,
    }


def markdown(summary: dict) -> str:
    def display(value: Any) -> str:
        return (
            "未提供 / 无有效值"
            if value is None
            else str(round(value, 6))
            if isinstance(value, float)
            else str(value)
        )

    lines = [
        "# 单次运行分析",
        "",
        f"- 运行：`{summary['run_id']}`",
        f"- 数据来源：`{summary['source_kind']}`",
        f"- 总时长：{display(summary['wall_seconds'])} s",
        f"- 确认有效时长：{display(summary['active_seconds'])} s",
        "- 本摘要不判定整机用例通过。",
        "",
        "| 关节 | 有效误差样本 | RMS / rad | 绝对峰值 / rad | 全程最高温度 / ℃ |",
        "|---|---:|---:|---:|---:|",
    ]
    for item in summary["joints"]:
        name = item["joint_name"].replace("|", "\\|").replace("\n", " ")
        error = item["tracking_error"]
        lines.append(
            f"| {name} | {error['sample_count']} | {display(error['rms'])} | {display(error['absolute_peak'])} | {display(item['temperature']['peak'])} |"
        )
    if summary["warnings"]:
        lines += ["", "## 数据说明", ""] + [f"- {item}" for item in summary["warnings"]]
    lines += ["", "详细缺失计数、事件和输入指纹见 `summary.json`。", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--max-gap-s",
        type=float,
        help="explicit maximum interval between joint samples",
    )
    args = parser.parse_args(argv)
    try:
        if args.output.resolve() == args.run_dir.resolve():
            raise ValueError("output must be separate from the raw run directory")
        summary = analyze(args.run_dir, max_gap_s=args.max_gap_s)
        args.output.mkdir(parents=True, exist_ok=True)
        (args.output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        (args.output / "report.md").write_text(markdown(summary), encoding="utf-8")
    except (ValueError, OSError, OverflowError) as exc:
        parser.exit(2, f"error: {exc}\n")
    print(f"summary: {args.output / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
