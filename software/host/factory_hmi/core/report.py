"""Offline execution-bundle finalization for the factory aging HMI."""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import json
import math
import os
import shutil
import statistics
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml


SCHEMA_VERSION = "1.0"
BUNDLE_FORMAT = "rp1-execution-bundle"
SUMMARY_PATH = "analysis/summary.json"
TELEMETRY_PATH = "telemetry/raw.csv"
_FILE_ROLES = {
    TELEMETRY_PATH: "telemetry",
    SUMMARY_PATH: "summary",
    "analysis/report.md": "report",
    "logs/native.log": "log",
    "config/station.yaml": "config",
    "config/execution.json": "config",
    "trajectory/metadata.json": "trajectory_metadata",
}


@dataclass(frozen=True)
class ReportResult:
    execution_dir: Path
    manifest_path: Path
    summary_path: Path
    report_path: Path
    bundle_path: Path
    manifest: dict[str, Any]
    summary: dict[str, Any]


def _utc_iso(value: Any = None) -> str:
    if isinstance(value, str) and value.strip():
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds_between(started_at: str, ended_at: str) -> float:
    start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    return max(0.0, (end - start).total_seconds())


def _safe_test_id(value: str) -> str:
    value = value.strip()
    if not value or value in {".", ".."}:
        raise ValueError("test_id must not be empty")
    if any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in value):
        raise ValueError("test_id may contain only letters, digits, dot, underscore, and dash")
    return value


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_fsynced(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _quantile(values: Iterable[float], quantile: float) -> float | None:
    ordered = sorted(float(value) for value in values if math.isfinite(float(value)))
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * float(quantile)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _maximum(values: Iterable[float | None]) -> float | None:
    finite = [float(value) for value in values if value is not None and math.isfinite(float(value))]
    return max(finite) if finite else None


def _metric_rule(
    name: str,
    actual: float | int | str | None,
    limit: float | int | str | None,
    unit: str,
    *,
    passed: bool | None,
    detail: str = "",
) -> dict[str, Any]:
    return {
        "name": name,
        "result": (
            "not_evaluated"
            if passed is None
            else ("passed" if passed else "failed")
        ),
        "actual": actual,
        "limit": limit,
        "unit": unit,
        "detail": detail,
    }


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]], list[float], bool]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no CSV header")
        fields = list(reader.fieldnames)
        rows = list(reader)
    times: list[float] = []
    non_monotonic = False
    previous: float | None = None
    for row in rows:
        value = _finite(row.get("time_s"))
        if value is None:
            continue
        if previous is not None and value <= previous:
            non_monotonic = True
        previous = value
        times.append(value)
    return fields, rows, times, non_monotonic


def _joint_names(fields: Iterable[str]) -> list[str]:
    names = [
        field[: -len("_cmd_pos_rad")]
        for field in fields
        if field.endswith("_cmd_pos_rad")
    ]
    if names:
        return names
    return [
        field[: -len("_pos_rad")]
        for field in fields
        if field.endswith("_pos_rad") and not field.endswith("_cmd_pos_rad")
    ]


def _entry_map(context: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    entries = context.get("entries")
    if not isinstance(entries, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(entries):
        if not isinstance(raw, Mapping):
            continue
        name = str(raw.get("joint_name", "")).strip()
        if name:
            result[name] = {
                "motor_id": int(raw.get("motor_id", index)),
                "module_name": str(
                    raw.get("module_name")
                    or context.get("module_name")
                    or context.get("limb")
                    or "unknown"
                ),
            }
    return result


def _joint_metrics(
    fields: list[str],
    rows: list[dict[str, str]],
    times: list[float],
    context: Mapping[str, Any],
    expected_samples: int,
    max_gap: float,
) -> list[dict[str, Any]]:
    names = _joint_names(fields)
    if not names:
        raise ValueError("telemetry CSV contains no joint position columns")
    entries = _entry_map(context)
    acceptance = context.get("acceptance")
    if not isinstance(acceptance, Mapping):
        acceptance = {}
    ratings = acceptance.get("rated_torque_nm")
    if not isinstance(ratings, Mapping):
        ratings = {}
    active_seconds = max(0.0, float(context.get("active_seconds") or 0.0))
    stable_window = min(60.0, active_seconds * 0.1)
    valid_times = sorted(times)
    first_time = valid_times[0] if valid_times else 0.0
    last_time = valid_times[-1] if valid_times else first_time
    result: list[dict[str, Any]] = []
    for index, name in enumerate(names):
        samples: list[tuple[float, dict[str, str]]] = []
        for row in rows:
            timestamp = _finite(row.get("time_s"))
            if timestamp is not None:
                samples.append((timestamp, row))
        temperatures = [
            value
            for _, row in samples
            if (value := _finite(row.get(f"{name}_temp_c"))) is not None
        ]
        start_temperatures = [
            value
            for timestamp, row in samples
            if timestamp <= first_time + stable_window
            and (value := _finite(row.get(f"{name}_temp_c"))) is not None
        ]
        end_temperatures = [
            value
            for timestamp, row in samples
            if timestamp >= last_time - stable_window
            and (value := _finite(row.get(f"{name}_temp_c"))) is not None
        ]
        start_temperature = (
            float(statistics.median(start_temperatures))
            if start_temperatures
            else None
        )
        end_temperature = (
            float(statistics.median(end_temperatures))
            if end_temperatures
            else None
        )
        peak_temperature = max(temperatures) if temperatures else None
        torques = [
            abs(value)
            for _, row in samples
            if (value := _finite(row.get(f"{name}_torque_nm"))) is not None
        ]
        tracking = [
            command - position
            for _, row in samples
            if (command := _finite(row.get(f"{name}_cmd_pos_rad"))) is not None
            and (position := _finite(row.get(f"{name}_pos_rad"))) is not None
        ]
        fault_count = 0
        active_fault = False
        for _, row in samples:
            error = _finite(row.get(f"{name}_error_id"))
            fault = error is not None and int(error) != 0
            if fault and not active_fault:
                fault_count += 1
            active_fault = fault
        entry = entries.get(name, {})
        rated = _finite(ratings.get(name))
        p999 = _quantile(torques, 0.999)
        result.append(
            {
                "module_name": str(
                    entry.get("module_name")
                    or context.get("module_name")
                    or context.get("limb")
                    or "unknown"
                ),
                "joint_name": name,
                "motor_id": int(entry.get("motor_id", index)),
                "sample_count": len(samples),
                "sample_completeness": min(
                    1.0,
                    len(samples) / expected_samples if expected_samples else 0.0,
                ),
                "max_gap_seconds": max_gap,
                "temperature_start_c": start_temperature,
                "temperature_end_c": end_temperature,
                "temperature_peak_c": peak_temperature,
                "temperature_rise_c": (
                    end_temperature - start_temperature
                    if start_temperature is not None and end_temperature is not None
                    else None
                ),
                "temperature_peak_rise_c": (
                    peak_temperature - start_temperature
                    if start_temperature is not None and peak_temperature is not None
                    else None
                ),
                "torque_peak_abs_nm": max(torques) if torques else None,
                "torque_p999_abs_nm": p999,
                "torque_mean_abs_nm": (
                    sum(torques) / len(torques) if torques else None
                ),
                "rated_torque_nm": rated,
                "torque_utilization": (
                    p999 / rated
                    if p999 is not None and rated is not None and rated > 0.0
                    else None
                ),
                "tracking_rms_rad": (
                    math.sqrt(sum(value * value for value in tracking) / len(tracking))
                    if tracking
                    else None
                ),
                "tracking_max_rad": (
                    max(abs(value) for value in tracking) if tracking else None
                ),
                "fault_count": fault_count,
            }
        )
    return result


def _build_verdict(
    context: Mapping[str, Any],
    quality: Mapping[str, Any],
    joints: list[dict[str, Any]],
    *,
    non_monotonic: bool,
) -> dict[str, Any]:
    acceptance = context.get("acceptance")
    if not isinstance(acceptance, Mapping):
        acceptance = {}
    criteria_version = str(acceptance.get("criteria_version") or "UNCONFIGURED")
    rules: list[dict[str, Any]] = []
    blocked_names: set[str] = set()
    failed_names: set[str] = set()

    def minimum(name: str, actual: float | int, key: str, unit: str) -> None:
        limit = _finite(acceptance.get(key))
        passed = None if limit is None else float(actual) >= limit
        rules.append(_metric_rule(name, actual, limit, unit, passed=passed))
        if passed is False:
            blocked_names.add(name)

    def maximum(
        name: str,
        actual: float | None,
        key: str,
        unit: str,
        *,
        product_failure: bool,
    ) -> None:
        limit = _finite(acceptance.get(key))
        passed = None if actual is None or limit is None else actual <= limit
        rules.append(_metric_rule(name, actual, limit, unit, passed=passed))
        if passed is False:
            (failed_names if product_failure else blocked_names).add(name)

    minimum(
        "required_active_seconds",
        float(context.get("active_seconds") or 0.0),
        "required_active_seconds",
        "s",
    )
    minimum(
        "required_cycles",
        int(context.get("completed_cycles") or 0),
        "required_cycles",
        "cycles",
    )
    minimum(
        "sample_completeness",
        float(quality["sample_completeness"]),
        "min_sample_completeness",
        "ratio",
    )
    maximum(
        "max_gap_seconds",
        float(quality["max_gap_seconds"]),
        "max_gap_seconds",
        "s",
        product_failure=False,
    )
    monotonic_rule = _metric_rule(
        "monotonic_time",
        "non_monotonic" if non_monotonic else "monotonic",
        "monotonic",
        "",
        passed=not non_monotonic,
    )
    rules.append(monotonic_rule)
    if non_monotonic:
        blocked_names.add("monotonic_time")

    for joint in joints:
        name = str(joint["joint_name"])
        maximum(
            f"{name}.temperature_rise_c",
            joint["temperature_rise_c"],
            "max_temperature_rise_c",
            "°C",
            product_failure=True,
        )
        maximum(
            f"{name}.torque_p999_utilization",
            joint["torque_utilization"],
            "max_torque_p999_utilization",
            "ratio",
            product_failure=True,
        )
        maximum(
            f"{name}.tracking_rms_rad",
            joint["tracking_rms_rad"],
            "max_tracking_rms_rad",
            "rad",
            product_failure=True,
        )
        fault_name = f"{name}.fault_intervals"
        no_faults = int(joint["fault_count"]) == 0
        rules.append(
            _metric_rule(
                fault_name,
                int(joint["fault_count"]),
                0,
                "intervals",
                passed=no_faults,
                detail="non-zero drive faults are motor safety failures",
            )
        )
        if not no_faults:
            failed_names.add(fault_name)

    explicit_failure = bool(
        context.get("product_failure") or context.get("safety_failure")
    )
    rules.append(
        _metric_rule(
            "terminal_safety_or_product_failure",
            "true" if explicit_failure else "false",
            "false",
            "",
            passed=not explicit_failure,
        )
    )
    if explicit_failure:
        failed_names.add("terminal_safety_or_product_failure")

    outcome = str(context.get("outcome") or "")
    interrupted = bool(context.get("manual_stop")) or outcome in {
        "stopped",
        "fault",
        "interrupted",
    }
    if interrupted and not explicit_failure:
        blocked_names.add("execution_interrupted")
        rules.append(
            _metric_rule(
                "execution_interrupted",
                outcome or "manual_stop",
                "completed",
                "",
                passed=False,
                detail=str(context.get("stop_reason") or "execution did not complete"),
            )
        )

    if failed_names:
        status = "failed"
        reasons = [f"failed rule: {name}" for name in sorted(failed_names)]
    elif blocked_names:
        status = "blocked"
        reasons = [f"blocked rule: {name}" for name in sorted(blocked_names)]
    else:
        status = "passed"
        reasons = []
    return {
        "status": status,
        "criteria_version": criteria_version,
        "reasons": reasons,
        "rules": rules,
    }


def _module_metrics(
    joints: list[dict[str, Any]],
    *,
    active_seconds: float,
    quality_acceptable: bool,
) -> list[dict[str, Any]]:
    by_module: dict[str, list[dict[str, Any]]] = {}
    for joint in joints:
        by_module.setdefault(str(joint["module_name"]), []).append(joint)
    result = []
    for module_name in sorted(by_module):
        items = by_module[module_name]

        def score(item: Mapping[str, Any]) -> tuple[float, str]:
            values = (
                item.get("temperature_peak_rise_c"),
                item.get("torque_utilization"),
                item.get("tracking_rms_rad"),
                float(item.get("fault_count") or 0),
            )
            return (
                max(
                    (float(value) for value in values if value is not None),
                    default=0.0,
                ),
                str(item["joint_name"]),
            )

        worst = max(items, key=score)
        result.append(
            {
                "module_name": module_name,
                "active_seconds": active_seconds,
                "effective_aging_hours": (
                    active_seconds / 3600.0 if quality_acceptable else 0.0
                ),
                "worst_joint": str(worst["joint_name"]),
                "max_temperature_rise_c": _maximum(
                    item["temperature_rise_c"] for item in items
                ),
                "max_torque_utilization": _maximum(
                    item["torque_utilization"] for item in items
                ),
                "max_tracking_rms_rad": _maximum(
                    item["tracking_rms_rad"] for item in items
                ),
                "fault_count": sum(int(item["fault_count"]) for item in items),
            }
        )
    return result


def _build_summary(
    csv_path: Path,
    context: Mapping[str, Any],
) -> dict[str, Any]:
    fields, rows, times, non_monotonic = _read_csv(csv_path)
    active_seconds = max(0.0, float(context.get("active_seconds") or 0.0))
    record_rate_hz = max(0.0, float(context.get("record_rate_hz") or 0.0))
    expected = math.floor(active_seconds * record_rate_hz) + 1
    valid_time_rows = len(times)
    completeness = min(
        1.0,
        valid_time_rows / expected if expected > 0 else 0.0,
    )
    ordered_times = sorted(times)
    max_gap = max(
        (
            max(0.0, current - previous)
            for previous, current in zip(ordered_times, ordered_times[1:])
        ),
        default=0.0,
    )
    quality = {
        "sample_count": valid_time_rows,
        "expected_sample_count": expected,
        "sample_completeness": completeness,
        "max_gap_seconds": max_gap,
    }
    joints = _joint_metrics(
        fields,
        rows,
        times,
        context,
        expected,
        max_gap,
    )
    verdict = _build_verdict(
        context,
        quality,
        joints,
        non_monotonic=non_monotonic or not times,
    )
    acceptance = context.get("acceptance")
    if not isinstance(acceptance, Mapping):
        acceptance = {}
    minimum = _finite(acceptance.get("min_sample_completeness"))
    maximum_gap = _finite(acceptance.get("max_gap_seconds"))
    quality_acceptable = (
        bool(times)
        and not non_monotonic
        and minimum is not None
        and completeness >= minimum
        and maximum_gap is not None
        and max_gap <= maximum_gap
    )
    started_at = _utc_iso(context.get("started_at"))
    ended_at = _utc_iso(context.get("ended_at"))
    wall_seconds = _seconds_between(started_at, ended_at)
    return {
        "schema_version": SCHEMA_VERSION,
        "execution_uuid": str(context["execution_uuid"]),
        "test_id": str(context["test_id"]),
        "timing": {
            "started_at": started_at,
            "ended_at": ended_at,
            "wall_seconds": wall_seconds,
            "active_seconds": active_seconds,
            "paused_seconds": max(0.0, wall_seconds - active_seconds),
            "completed_cycles": max(
                0, int(context.get("completed_cycles") or 0)
            ),
        },
        "quality": quality,
        "verdict": verdict,
        "module_metrics": _module_metrics(
            joints,
            active_seconds=active_seconds,
            quality_acceptable=quality_acceptable,
        ),
        "joint_metrics": joints,
    }


def _render_report(summary: Mapping[str, Any], manifest_identity: Mapping[str, Any]) -> str:
    timing = summary["timing"]
    quality = summary["quality"]
    verdict = summary["verdict"]
    lines = [
        f"# Factory aging report: {summary['test_id']}",
        "",
        f"- Execution UUID: `{summary['execution_uuid']}`",
        f"- Test case: `{manifest_identity['test_case_id']}`",
        f"- Sample: `{manifest_identity['sample_id']}`",
        f"- Bench/station: `{manifest_identity['bench_id']}` / `{manifest_identity.get('station_id', '')}`",
        f"- Operator: `{manifest_identity.get('operator_id', '')}`",
        f"- Started: {timing['started_at']}",
        f"- Ended: {timing['ended_at']}",
        f"- Active / wall / paused seconds: {timing['active_seconds']:.3f} / {timing['wall_seconds']:.3f} / {timing['paused_seconds']:.3f}",
        f"- Completed cycles: {timing['completed_cycles']}",
        "",
        f"## Verdict: {str(verdict['status']).upper()}",
        "",
        f"Criteria version: `{verdict['criteria_version']}`",
        "",
    ]
    if verdict["reasons"]:
        lines.extend(f"- {reason}" for reason in verdict["reasons"])
    else:
        lines.append("- All configured acceptance rules passed.")
    lines.extend(
        [
            "",
            "## Sampling quality",
            "",
            f"- Samples: {quality['sample_count']} / {quality['expected_sample_count']}",
            f"- Completeness: {quality['sample_completeness']:.6f}",
            f"- Maximum gap: {quality['max_gap_seconds']:.6f} s",
            "",
            "## Joint metrics",
            "",
        ]
    )
    for joint in summary["joint_metrics"]:
        lines.extend(
            [
                f"### {joint['joint_name']}",
                f"- Temperature start/end/peak: {joint['temperature_start_c']} / {joint['temperature_end_c']} / {joint['temperature_peak_c']} °C",
                f"- Temperature end/peak rise: {joint['temperature_rise_c']} / {joint['temperature_peak_rise_c']} °C",
                f"- Torque peak/P99.9/mean absolute: {joint['torque_peak_abs_nm']} / {joint['torque_p999_abs_nm']} / {joint['torque_mean_abs_nm']} N·m",
                f"- Rated torque/utilization: {joint['rated_torque_nm']} N·m / {joint['torque_utilization']}",
                f"- Tracking RMS/max: {joint['tracking_rms_rad']} / {joint['tracking_max_rad']} rad",
                f"- Fault intervals: {joint['fault_count']}",
                "",
            ]
        )
    lines.extend(["## Acceptance rules", ""])
    for rule in verdict["rules"]:
        lines.append(
            f"- **{rule['result']}** `{rule['name']}`: actual={rule.get('actual')} limit={rule.get('limit')} {rule.get('unit', '')}".rstrip()
        )
    return "\n".join(lines) + "\n"


def _deterministic_tar_gz(source: Path, destination: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source).as_posix()
            info = tarfile.TarInfo(relative)
            info.size = path.stat().st_size
            info.mode = 0o640
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.mtime = 0
            with path.open("rb") as handle:
                archive.addfile(info, handle)
    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=compressed,
        mtime=0,
        compresslevel=9,
    ) as handle:
        handle.write(buffer.getvalue())
    _write_fsynced(destination, compressed.getvalue())


class ReportManager:
    """Create and rebuild v1 bundles without requiring motor hardware."""

    def __init__(self, data_root: Path | str) -> None:
        self.data_root = Path(data_root).expanduser().resolve()
        self.executions_root = self.data_root / "executions"
        self.exports_root = self.data_root / "exports"
        self.executions_root.mkdir(parents=True, exist_ok=True)
        self.exports_root.mkdir(parents=True, exist_ok=True)

    def finalize(
        self,
        csv_path: Path | str,
        context: Mapping[str, Any],
        *,
        native_log: Path | str | None = None,
    ) -> ReportResult:
        source = Path(csv_path).expanduser().resolve(strict=True)
        if not source.is_file() or source.is_symlink():
            raise ValueError("telemetry source must be a regular file")
        normalized = self._normalize_context(context)
        test_id = _safe_test_id(str(normalized["test_id"]))
        destination = self.executions_root / test_id
        temporary = Path(
            tempfile.mkdtemp(prefix=f".{test_id}.", dir=self.executions_root)
        )
        try:
            telemetry = temporary / TELEMETRY_PATH
            telemetry.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, telemetry)
            with telemetry.open("rb") as handle:
                os.fsync(handle.fileno())

            station_config = normalized.get("config")
            if not isinstance(station_config, Mapping):
                station_config = {}
            config_bytes = yaml.safe_dump(
                dict(station_config),
                allow_unicode=True,
                sort_keys=True,
            ).encode("utf-8")
            _write_fsynced(temporary / "config/station.yaml", config_bytes)
            _write_fsynced(
                temporary / "config/execution.json",
                _json_bytes(self._rebuild_context(normalized)),
            )

            trajectory = normalized.get("trajectory")
            if not isinstance(trajectory, Mapping):
                trajectory = {}
            trajectory_metadata = dict(trajectory)
            if trajectory_metadata.get("path"):
                trajectory_metadata["filename"] = Path(
                    str(trajectory_metadata.pop("path"))
                ).name
            _write_fsynced(
                temporary / "trajectory/metadata.json",
                _json_bytes(trajectory_metadata),
            )

            if native_log is not None:
                log_source = Path(native_log).expanduser().resolve(strict=True)
                if log_source.is_file() and not log_source.is_symlink():
                    target = temporary / "logs/native.log"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(log_source, target)

            summary = _build_summary(telemetry, normalized)
            _write_fsynced(temporary / SUMMARY_PATH, _json_bytes(summary))

            identity = self._manifest_identity(normalized, summary)
            report = _render_report(summary, identity)
            _write_fsynced(
                temporary / "analysis/report.md",
                report.encode("utf-8"),
            )

            files = []
            for path in sorted(
                item
                for item in temporary.rglob("*")
                if item.is_file() and item.name != "manifest.json"
            ):
                if path.is_symlink():
                    raise ValueError("bundle must not contain symbolic links")
                relative = path.relative_to(temporary).as_posix()
                files.append(
                    {
                        "path": relative,
                        "role": _FILE_ROLES.get(relative, "other"),
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
            manifest = {
                **identity,
                "files": files,
            }
            _write_fsynced(temporary / "manifest.json", _json_bytes(manifest))
            _fsync_directory(temporary)

            backup = destination.with_name(f".{destination.name}.previous")
            if backup.exists():
                shutil.rmtree(backup)
            if destination.exists():
                os.replace(destination, backup)
            os.replace(temporary, destination)
            _fsync_directory(self.executions_root)
            if backup.exists():
                shutil.rmtree(backup)

            bundle_path = self.exports_root / f"{test_id}.tar.gz"
            _deterministic_tar_gz(destination, bundle_path)
            _fsync_directory(self.exports_root)
            return ReportResult(
                execution_dir=destination,
                manifest_path=destination / "manifest.json",
                summary_path=destination / SUMMARY_PATH,
                report_path=destination / "analysis/report.md",
                bundle_path=bundle_path,
                manifest=manifest,
                summary=summary,
            )
        except Exception:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
            raise

    def rebuild(self, test_id: str) -> ReportResult:
        execution = self.execution_dir(test_id)
        context_path = execution / "config/execution.json"
        if context_path.is_file():
            context = json.loads(context_path.read_text(encoding="utf-8"))
        else:
            manifest = self.manifest(test_id)
            summary = json.loads(
                (execution / SUMMARY_PATH).read_text(encoding="utf-8")
            )
            context = {
                **manifest,
                "active_seconds": summary["timing"]["active_seconds"],
                "completed_cycles": summary["timing"]["completed_cycles"],
                "record_rate_hz": (
                    summary["quality"]["expected_sample_count"] - 1
                )
                / max(1.0, summary["timing"]["active_seconds"]),
                "outcome": "completed",
            }
        config_path = execution / "config/station.yaml"
        context["config"] = (
            yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            if config_path.is_file()
            else {}
        )
        context["acceptance"] = context["config"].get(
            "factory_hmi_acceptance", {}
        )
        metadata_path = execution / "trajectory/metadata.json"
        context["trajectory"] = (
            json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata_path.is_file()
            else {}
        )
        log_path = execution / "logs/native.log"
        return self.finalize(
            execution / TELEMETRY_PATH,
            context,
            native_log=log_path if log_path.is_file() else None,
        )

    def recover_orphans(self, record_root: Path | str) -> list[ReportResult]:
        results: list[ReportResult] = []
        for csv_path in sorted(Path(record_root).glob("*.csv")):
            test_id = csv_path.stem
            if (self.executions_root / test_id / "manifest.json").is_file():
                continue
            sidecar = csv_path.with_suffix(".context.json")
            if sidecar.is_file():
                context = json.loads(sidecar.read_text(encoding="utf-8"))
            else:
                _, _, times, _ = _read_csv(csv_path)
                stat = csv_path.stat()
                active = max(times, default=0.0)
                ended = _utc_iso(stat.st_mtime)
                started = _utc_iso(stat.st_mtime - active)
                context = {
                    "execution_uuid": str(
                        uuid.uuid5(
                            uuid.NAMESPACE_URL,
                            f"rp1-factory-hmi:{test_id}:{_sha256(csv_path)}",
                        )
                    ),
                    "test_id": test_id,
                    "test_case_id": "recovered-csv",
                    "sample_id": "unknown",
                    "bench_id": "unknown",
                    "station_id": "",
                    "operator_id": "",
                    "started_at": started,
                    "ended_at": ended,
                    "active_seconds": active,
                    "completed_cycles": 0,
                    "record_rate_hz": (
                        (len(times) - 1) / active
                        if len(times) > 1 and active > 0.0
                        else 0.0
                    ),
                    "outcome": "interrupted",
                    "manual_stop": True,
                    "config": {},
                    "acceptance": {},
                    "trajectory": {},
                }
            results.append(self.finalize(csv_path, context))
        return results

    def execution_dir(self, test_id: str) -> Path:
        path = (self.executions_root / _safe_test_id(test_id)).resolve()
        if path.parent != self.executions_root or not path.is_dir():
            raise FileNotFoundError(f"execution not found: {test_id}")
        return path

    def manifest(self, test_id: str) -> dict[str, Any]:
        return json.loads(
            (self.execution_dir(test_id) / "manifest.json").read_text(
                encoding="utf-8"
            )
        )

    def summary(self, test_id: str) -> dict[str, Any]:
        return json.loads(
            (self.execution_dir(test_id) / SUMMARY_PATH).read_text(
                encoding="utf-8"
            )
        )

    def report_path(self, test_id: str) -> Path:
        path = self.execution_dir(test_id) / "analysis/report.md"
        if not path.is_file():
            raise FileNotFoundError(f"report not found: {test_id}")
        return path

    def bundle_path(self, test_id: str) -> Path:
        path = (self.exports_root / f"{_safe_test_id(test_id)}.tar.gz").resolve()
        if path.parent != self.exports_root or not path.is_file():
            raise FileNotFoundError(f"bundle not found: {test_id}")
        return path

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        items = []
        for path in sorted(
            self.executions_root.glob("*/manifest.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        ):
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                summary = json.loads(
                    (path.parent / SUMMARY_PATH).read_text(encoding="utf-8")
                )
            except (OSError, ValueError, KeyError):
                continue
            items.append(
                {
                    "execution_uuid": manifest["execution_uuid"],
                    "test_id": manifest["test_id"],
                    "sample_id": manifest["sample_id"],
                    "station_id": manifest.get("station_id", ""),
                    "operator_id": manifest.get("operator_id", ""),
                    "started_at": manifest["started_at"],
                    "ended_at": manifest["ended_at"],
                    "verdict": summary["verdict"]["status"],
                    "bundle_path": str(
                        self.exports_root / f"{manifest['test_id']}.tar.gz"
                    ),
                }
            )
            if len(items) >= max(1, int(limit)):
                break
        return items

    @staticmethod
    def _normalize_context(context: Mapping[str, Any]) -> dict[str, Any]:
        value = dict(context)
        execution_uuid = str(value.get("execution_uuid") or uuid.uuid4())
        uuid.UUID(execution_uuid)
        value["execution_uuid"] = execution_uuid
        value["test_id"] = _safe_test_id(str(value.get("test_id") or ""))
        value["test_case_id"] = str(
            value.get("test_case_id") or value.get("limb") or "factory-aging"
        )
        value["sample_id"] = str(
            value.get("sample_id") or value.get("robot_id") or "unknown"
        )
        value["bench_id"] = str(
            value.get("bench_id") or value.get("station_id") or "unknown"
        )
        value["started_at"] = _utc_iso(value.get("started_at"))
        value["ended_at"] = _utc_iso(
            value.get("ended_at") or value["started_at"]
        )
        config = value.get("config")
        if not isinstance(config, Mapping):
            config = {}
        value["config"] = dict(config)
        acceptance = value.get("acceptance")
        if not isinstance(acceptance, Mapping):
            acceptance = config.get("factory_hmi_acceptance", {})
        value["acceptance"] = (
            dict(acceptance) if isinstance(acceptance, Mapping) else {}
        )
        return value

    @staticmethod
    def _rebuild_context(context: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {
            "execution_uuid",
            "test_id",
            "campaign_id",
            "test_case_id",
            "test_case_version",
            "sample_id",
            "bench_id",
            "robot_id",
            "station_id",
            "operator_id",
            "started_at",
            "ended_at",
            "active_seconds",
            "completed_cycles",
            "record_rate_hz",
            "outcome",
            "manual_stop",
            "product_failure",
            "safety_failure",
            "stop_reason",
            "module_name",
            "limb",
            "entries",
            "software",
            "calibration_version",
        }
        return {
            key: value
            for key, value in context.items()
            if key in allowed
        }

    @staticmethod
    def _manifest_identity(
        context: Mapping[str, Any],
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        config_bytes = yaml.safe_dump(
            dict(context.get("config") or {}),
            allow_unicode=True,
            sort_keys=True,
        ).encode("utf-8")
        trajectory = context.get("trajectory")
        trajectory_sha = (
            str(trajectory.get("sha256") or "")
            if isinstance(trajectory, Mapping)
            else ""
        )
        identity: dict[str, Any] = {
            "bundle_format": BUNDLE_FORMAT,
            "schema_version": SCHEMA_VERSION,
            "execution_uuid": str(context["execution_uuid"]),
            "test_id": str(context["test_id"]),
            "campaign_id": (
                str(context["campaign_id"])
                if context.get("campaign_id") is not None
                else None
            ),
            "test_case_id": str(context["test_case_id"]),
            "test_case_version": str(context.get("test_case_version") or ""),
            "sample_id": str(context["sample_id"]),
            "bench_id": str(context["bench_id"]),
            "robot_id": str(context.get("robot_id") or ""),
            "station_id": str(context.get("station_id") or ""),
            "operator_id": str(context.get("operator_id") or ""),
            "status": str(summary["verdict"]["status"]),
            "started_at": str(summary["timing"]["started_at"]),
            "ended_at": str(summary["timing"]["ended_at"]),
            "generated_at": str(summary["timing"]["ended_at"]),
            "summary_path": SUMMARY_PATH,
            "telemetry_path": TELEMETRY_PATH,
            "software": dict(context.get("software") or {}),
            "provenance": {
                "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
                "trajectory_sha256": trajectory_sha,
                "calibration_version": str(
                    context.get("calibration_version") or ""
                ),
            },
        }
        return identity


__all__ = [
    "BUNDLE_FORMAT",
    "ReportManager",
    "ReportResult",
    "SCHEMA_VERSION",
]
