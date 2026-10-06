#!/usr/bin/env python3
"""Log motor telemetry to CSV on hardware, or live-plot sim joint angles from CSV.

Hardware (integrated with replay — recommended):

  python3 scripts/replay_limb_traj.py \\
    --limb left_leg --config scripts/config/leg_motors.yaml \\
    --trajectory outputs/test.npz \\
    --record --record-output outputs/telemetry.csv

Optional live InfluxDB sync (same Recorder; no token = CSV only):

  export INFLUXDB_URL=http://localhost:8181
  export INFLUXDB_TOKEN=your_token
  export INFLUXDB_DB=robot_test
  export ROBOT_ID=RP1-001
  export TEST_ID=reliab-$(date +%Y%m%d-%H%M%S)
  # optional: INFLUX_SAMPLE_STRIDE=10  INFLUX_FLUSH_INTERVAL_S=0.2  INFLUX_FLUSH_MAX_LINES=40

Hardware (standalone passive listen, two terminals):

  # terminal 1: trajectory replay
  python3 scripts/replay_limb_traj.py --limb left_leg --trajectory outputs/test.npz

  # terminal 2: passive logging (no MIT commands, no motor enable/disable)
  python3 scripts/log_motor_telemetry.py \\
    --limb left_leg --config scripts/config/limb_motors.yaml \\
    --output outputs/telemetry.csv --rate 200 --trajectory outputs/test.npz

Simulation (two terminals):

  # terminal 1: sim loop replay with joint CSV log
  python3 scripts/replay_sim_limb_traj_loop.py \\
    --trajectory outputs/right_short_arm_1.npz \\
    --limb right_short_arm --config scripts/config/right_short_arm_motors.yaml \\
    --joint-log outputs/sim_joint_log.csv

  # terminal 2: live joint angle plot
  python3 scripts/log_motor_telemetry.py \\
    --sim-live --joint-csv outputs/sim_joint_log.csv
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import os
import queue
import shutil
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

try:
    from scripts.rp1_limb_common import (
        DEFAULT_CONFIG,
        MotorEntry,
        add_motors_python_paths,
        canonical_limb,
        create_motor_driver,
        limb_motor_entries,
        load_motor_position_npz,
        load_yaml_config,
        positive_float,
        print_motor_table,
        trajectory_target_at,
    )
except ModuleNotFoundError:
    from rp1_limb_common import (
        DEFAULT_CONFIG,
        MotorEntry,
        add_motors_python_paths,
        canonical_limb,
        create_motor_driver,
        limb_motor_entries,
        load_motor_position_npz,
        load_yaml_config,
        positive_float,
        print_motor_table,
        trajectory_target_at,
    )


@contextlib.contextmanager
def redirect_native_stderr(target_path: Path):
    """Redirect fd 2 (motors spdlog) so in-place terminal blocks are not garbled."""
    sys.stderr.flush()
    saved_fd = os.dup(2)
    log_fd = os.open(str(target_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.dup2(log_fd, 2)
        os.close(log_fd)
        yield
    finally:
        sys.stderr.flush()
        os.dup2(saved_fd, 2)
        os.close(saved_fd)


@dataclass(frozen=True)
class MotorSample:
    pos_rad: float
    cmd_pos_rad: float
    spd_rad_s: float
    torque_nm: float
    temp_c: float
    error_id: int
    bus_voltage_v: float = float("nan")
    bus_current_a: float = float("nan")


class ControllerHeartbeat:
    """Monotonic timestamp touched after each successful control dispatch."""

    def __init__(self) -> None:
        self._last_touch: float | None = None
        self._touch_count = 0
        self._lock = threading.Lock()

    def touch(self, now: float | None = None) -> None:
        with self._lock:
            self._last_touch = time.monotonic() if now is None else float(now)
            self._touch_count += 1

    def snapshot(self) -> tuple[int, float | None]:
        with self._lock:
            return self._touch_count, self._last_touch

    def age(self, now: float | None = None) -> float | None:
        with self._lock:
            last_touch = self._last_touch
        if last_touch is None:
            return None
        current = time.monotonic() if now is None else float(now)
        return max(0.0, current - last_touch)


def _read_optional_float(motor: Any, method_name: str) -> float:
    getter = getattr(motor, method_name, None)
    if getter is None:
        return float("nan")
    try:
        value = float(getter())
    except Exception:
        return float("nan")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--sim-live",
        action="store_true",
        help="live-plot sim joint angles from --joint-csv (no hardware)",
    )
    parser.add_argument(
        "--joint-csv",
        type=Path,
        help="sim joint log CSV written by replay_sim_limb_traj_loop.py --joint-log",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="limb motor config YAML")
    parser.add_argument("--limb", help="right_leg, left_leg, left_arm, right_arm, right_short_arm, ...")
    parser.add_argument("--output", type=Path, help="output CSV path (hardware mode)")
    parser.add_argument(
        "--rate",
        type=positive_float,
        default=200.0,
        help="hardware CSV sample rate in Hz, or sim plot refresh rate with --sim-live",
    )
    parser.add_argument(
        "--duration",
        type=positive_float,
        default=0.0,
        help="seconds to log; 0 means until Ctrl+C",
    )
    parser.add_argument(
        "--query",
        action="store_true",
        help="actively query each motor with refresh_motor_status(); "
        "use only when no other control script is running on the same bus",
    )
    parser.add_argument(
        "--trajectory",
        type=Path,
        help="optional .npz motor trajectory; logs MIT command position by elapsed time "
        "(use with replay_limb_traj in another terminal, press Enter to sync t=0)",
    )
    parser.add_argument(
        "--speed",
        type=positive_float,
        default=1.0,
        help="playback speed multiplier for --trajectory command position (default: 1.0)",
    )
    parser.add_argument(
        "--no-print",
        action="store_true",
        help="disable terminal status print (default: print at half the sample rate)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print planned columns and exit")
    args = parser.parse_args()
    if args.sim_live:
        if args.joint_csv is None:
            parser.error("--sim-live requires --joint-csv")
    else:
        if args.limb is None:
            parser.error("hardware mode requires --limb")
        if args.output is None:
            parser.error("hardware mode requires --output")
    return args


def read_motor_samples(motors, *, cmd_pos: list[float] | None = None) -> list[MotorSample]:
    samples: list[MotorSample] = []
    for index, motor in enumerate(motors):
        if cmd_pos is not None:
            cmd = float(cmd_pos[index])
        else:
            cmd = _read_motor_cmd_pos(motor)
        samples.append(
            MotorSample(
                pos_rad=float(motor.get_motor_pos()),
                cmd_pos_rad=cmd,
                spd_rad_s=float(motor.get_motor_spd()),
                torque_nm=float(motor.get_motor_current()),
                temp_c=float(motor.get_motor_temperature()),
                error_id=int(motor.get_error_id()),
                bus_voltage_v=_read_optional_float(motor, "get_motor_dc_bus_voltage"),
                bus_current_a=_read_optional_float(motor, "get_motor_dc_bus_current"),
            )
        )
    return samples


def _read_motor_cmd_pos(motor: Any) -> float:
    getter = getattr(motor, "get_motor_cmd_pos", None)
    if getter is None:
        return float("nan")
    return float(getter())


def csv_fieldnames(entries: list[MotorEntry]) -> list[str]:
    fields = ["time_s", "sample"]
    for entry in entries:
        prefix = entry.joint_name
        fields.extend(
            [
                f"{prefix}_cmd_pos_rad",
                f"{prefix}_pos_rad",
                f"{prefix}_spd_rad_s",
                f"{prefix}_torque_nm",
                f"{prefix}_temp_c",
                f"{prefix}_error_id",
                f"{prefix}_bus_voltage_v",
                f"{prefix}_bus_current_a",
            ]
        )
    return fields


def sample_row(
    entries: list[MotorEntry],
    samples: list[MotorSample],
    *,
    elapsed_s: float,
    sample: int,
) -> dict[str, float | int]:
    if len(samples) != len(entries):
        raise ValueError("sample count does not match motor entries")
    row: dict[str, float | int] = {
        "time_s": elapsed_s,
        "sample": sample,
    }
    for entry, item in zip(entries, samples):
        prefix = entry.joint_name
        row[f"{prefix}_cmd_pos_rad"] = item.cmd_pos_rad
        row[f"{prefix}_pos_rad"] = item.pos_rad
        row[f"{prefix}_spd_rad_s"] = item.spd_rad_s
        row[f"{prefix}_torque_nm"] = item.torque_nm
        row[f"{prefix}_temp_c"] = item.temp_c
        row[f"{prefix}_error_id"] = item.error_id
        row[f"{prefix}_bus_voltage_v"] = item.bus_voltage_v
        row[f"{prefix}_bus_current_a"] = item.bus_current_a
    return row


def cmd_pos_from_trajectory(
    motor_pos: np.ndarray,
    trajectory_time: np.ndarray,
    *,
    elapsed_s: float,
    speed: float,
) -> list[float]:
    target = trajectory_target_at(motor_pos, trajectory_time, elapsed_s * speed)
    return [float(value) for value in target.reshape(-1)]


def print_terminal_status(
    entries: list[MotorEntry],
    samples: list[MotorSample],
    *,
    elapsed_s: float,
) -> None:
    parts = [f"t={elapsed_s:.3f}s"]
    for entry, item in zip(entries, samples):
        label = entry.joint_name.replace("_joint", "").replace("right_", "r_").replace("left_", "l_")
        cmd_text = f" cmd={item.cmd_pos_rad:+.4f}" if math.isfinite(item.cmd_pos_rad) else ""
        parts.append(
            f"{label}: pos={item.pos_rad:+.4f}{cmd_text} "
            f"torque={item.torque_nm:+.2f}Nm temp={item.temp_c:.0f}C"
        )
    print(" | ".join(parts))


def short_joint_name(joint_name: str) -> str:
    return joint_name.replace("left_", "l_").replace("right_", "r_").replace("_joint", "")


LRO_ERROR_NAMES: dict[int, str] = {
    0x01: "OVERHEAT",
    0x02: "OVER_CURRENT",
    0x03: "UNDER_VOLTAGE",
    0x04: "ENCODER_ERROR",
    0x06: "BRAKE_OVERVOLT",
    0x07: "DRV_ERROR",
}


def motor_error_label(error_id: int) -> str:
    err = int(error_id)
    if err == 0:
        return "0"
    name = LRO_ERROR_NAMES.get(err)
    if name:
        return f"0x{err:x} ({name})"
    return f"0x{err:x}"


@dataclass(frozen=True)
class FaultRecord:
    motor_id: int
    joint_label: str
    error_id: int


class FaultLatch:
    """Remember motor faults so the live block keeps showing them after they clear."""

    def __init__(self) -> None:
        self._records: dict[int, FaultRecord] = {}

    def update(self, entries: list[MotorEntry], samples: list[MotorSample]) -> None:
        for entry, item in zip(entries, samples):
            err = int(item.error_id)
            if err == 0:
                continue
            self._records[int(entry.motor_id)] = FaultRecord(
                motor_id=int(entry.motor_id),
                joint_label=short_joint_name(entry.joint_name),
                error_id=err,
            )

    def records(self) -> list[FaultRecord]:
        return sorted(self._records.values(), key=lambda record: record.motor_id)

    def banner(self) -> str | None:
        records = self.records()
        if not records:
            return None
        parts = [
            f"id={record.motor_id}({record.joint_label}) {motor_error_label(record.error_id)}"
            for record in records
        ]
        text = "*** FAULT (latched): " + " | ".join(parts) + " ***"
        if sys.stdout.isatty():
            return f"\033[1;31m{text}\033[0m"
        return text


class LiveBlockDisplay:
    """Rewrite a fixed multi-line block in place (one line per joint)."""

    def __init__(self, *, use_alt_screen: bool = False) -> None:
        self._line_count = 0
        self._use_alt_screen = bool(use_alt_screen)
        self._alt_active = False

    def begin(self) -> None:
        if self._use_alt_screen and sys.stdout.isatty():
            sys.stdout.write("\033[?1049h\033[H\033[?25l")
            sys.stdout.flush()
            self._alt_active = True
            self._line_count = 0

    def _clip_block(self, block: list[str]) -> list[str]:
        width = max(40, shutil.get_terminal_size(fallback=(100, 24)).columns)
        return [line[:width] for line in block]

    def update(self, lines: list[str], *, footer: str | list[str]) -> None:
        footer_lines = [footer] if isinstance(footer, str) else list(footer)
        block = self._clip_block([*lines, *footer_lines])
        if self._alt_active:
            sys.stdout.write("\033[H")
            for line in block:
                sys.stdout.write("\033[2K")
                sys.stdout.write(line + "\n")
            if self._line_count > len(block):
                sys.stdout.write("\033[J")
            sys.stdout.flush()
        elif self._line_count == 0:
            for line in block:
                print(line, flush=True)
        else:
            sys.stdout.write(f"\033[{self._line_count}A")
            for line in block:
                sys.stdout.write("\033[2K\r")
                sys.stdout.write(line + "\n")
            sys.stdout.flush()
        self._line_count = len(block)

    def finish(self) -> None:
        if self._alt_active:
            sys.stdout.write("\033[?1049l\033[?25h")
            sys.stdout.flush()
            self._alt_active = False
        elif self._line_count:
            print(flush=True)
        self._line_count = 0


def telemetry_live_lines(
    entries: list[MotorEntry],
    samples: list[MotorSample],
    *,
    elapsed_s: float,
) -> list[str]:
    lines = [f"t={elapsed_s:.3f}s"]
    for entry, item in zip(entries, samples):
        label = short_joint_name(entry.joint_name)
        cmd_text = f"cmd {item.cmd_pos_rad:+.6f}  " if math.isfinite(item.cmd_pos_rad) else ""
        err = int(item.error_id)
        err_text = f"!! err {motor_error_label(err)} !!" if err else "err 0"
        lines.append(
            f"[{entry.index}] {label:<14} id={entry.motor_id}  "
            f"pos {item.pos_rad:+.6f}  {cmd_text}"
            f"torque {item.torque_nm:+.2f}Nm  temp {item.temp_c:.0f}C  "
            f"spd {item.spd_rad_s:+.4f}  {err_text}"
        )
    return lines


def telemetry_live_footer(*, rate_hz: float, fault_latch: FaultLatch | None = None) -> list[str]:
    lines: list[str] = []
    if fault_latch is not None:
        banner = fault_latch.banner()
        if banner is not None:
            lines.append(banner)
    lines.append(f"--- telemetry {rate_hz:.0f} Hz | Ctrl+C to stop ---")
    return lines


def collect_and_log(
    motors,
    entries: list[MotorEntry],
    writer: csv.DictWriter,
    *,
    rate_hz: float,
    duration_s: float,
    query: bool,
    motor_pos: np.ndarray | None = None,
    trajectory_time: np.ndarray | None = None,
    playback_speed: float = 1.0,
    print_status: bool = True,
) -> int:
    period = 1.0 / float(rate_hz)
    start = time.monotonic()
    row_count = 0
    sample_index = 0

    while True:
        loop_start = time.monotonic()
        elapsed = loop_start - start
        if duration_s > 0.0 and elapsed >= duration_s:
            break

        if query:
            for motor in motors:
                motor.refresh_motor_status()

        cmd_pos = None
        if motor_pos is not None and trajectory_time is not None:
            cmd_pos = cmd_pos_from_trajectory(
                motor_pos,
                trajectory_time,
                elapsed_s=elapsed,
                speed=playback_speed,
            )

        samples = read_motor_samples(motors, cmd_pos=cmd_pos)
        writer.writerow(
            sample_row(
                entries,
                samples,
                elapsed_s=elapsed,
                sample=sample_index,
            )
        )
        if print_status and sample_index % 2 == 0:
            print_terminal_status(entries, samples, elapsed_s=elapsed)
        row_count += 1
        sample_index += 1

        sleep_time = period - (time.monotonic() - loop_start)
        if sleep_time > 0.0:
            time.sleep(sleep_time)

    return row_count


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _lp_escape_tag(value: str) -> str:
    """Escape tag values for InfluxDB line protocol."""
    return (
        str(value)
        .replace(" ", "\\ ")
        .replace(",", "\\,")
        .replace("=", "\\=")
    )


def _lp_escape_string_field(value: str) -> str:
    """Escape a quoted string field for InfluxDB line protocol."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _lp_format_float(value: float) -> str | None:
    if not math.isfinite(value):
        return None
    return f"{float(value):.9g}"


def _lp_format_field_value(value: str | int | float | bool) -> str | None:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return f"{value}i"
    if isinstance(value, float):
        return _lp_format_float(value)
    return f'"{_lp_escape_string_field(value)}"'


def test_event_to_line_protocol(
    *,
    robot_id: str,
    test_id: str,
    limb: str,
    event_type: str,
    elapsed_s: float,
    timestamp_ns: int,
    joint: str | None = None,
    error_id: int | None = None,
    duration_s: float | None = None,
    fault_count: int | None = None,
) -> str:
    """Encode one execution lifecycle/fault event as Influx line protocol."""
    tags = [
        f"robot_id={_lp_escape_tag(robot_id)}",
        f"test_id={_lp_escape_tag(test_id)}",
        f"limb={_lp_escape_tag(limb)}",
        f"event_type={_lp_escape_tag(event_type)}",
    ]
    if joint is not None:
        tags.append(f"joint={_lp_escape_tag(joint)}")

    raw_fields: list[tuple[str, str | int | float | bool]] = [("elapsed_s", float(elapsed_s))]
    if error_id is not None:
        raw_fields.append(("error_id", int(error_id)))
    if duration_s is not None:
        raw_fields.append(("duration_s", float(duration_s)))
    if fault_count is not None:
        raw_fields.append(("fault_count", int(fault_count)))
    fields = [
        f"{name}={formatted}"
        for name, value in raw_fields
        if (formatted := _lp_format_field_value(value)) is not None
    ]
    return f"test_event,{','.join(tags)} {','.join(fields)} {int(timestamp_ns)}"


def samples_to_line_protocol(
    entries: list[MotorEntry],
    samples: list[MotorSample],
    *,
    robot_id: str,
    test_id: str,
    limb: str,
    timestamp_ns: int,
) -> list[str]:
    """One Line Protocol point per joint (measurement: joint_metrics)."""
    if len(samples) != len(entries):
        raise ValueError("sample count does not match motor entries")

    robot = _lp_escape_tag(robot_id)
    test = _lp_escape_tag(test_id)
    limb_tag = _lp_escape_tag(limb)
    lines: list[str] = []

    for entry, item in zip(entries, samples):
        joint = _lp_escape_tag(entry.joint_name)
        fields: list[str] = []
        for name, value in (
            ("pos_rad", item.pos_rad),
            ("cmd_pos_rad", item.cmd_pos_rad),
            ("spd_rad_s", item.spd_rad_s),
            ("torque_nm", item.torque_nm),
            ("temp_c", item.temp_c),
            ("bus_voltage_v", item.bus_voltage_v),
            ("bus_current_a", item.bus_current_a),
        ):
            formatted = _lp_format_float(value)
            if formatted is not None:
                fields.append(f"{name}={formatted}")
        fields.append(f"error_id={int(item.error_id)}i")
        if not fields:
            continue
        lines.append(
            f"joint_metrics,robot_id={robot},test_id={test},"
            f"limb={limb_tag},joint={joint} "
            f"{','.join(fields)} {timestamp_ns}"
        )
    return lines


class InfluxTelemetryWriter:
    """Background Line Protocol writer for InfluxDB 3.

    Enabled only when ``INFLUXDB_TOKEN`` is set. Sampling thread enqueues lines;
    a separate flush thread POSTs batches so CSV timing is not blocked by HTTP.
    """

    def __init__(
        self,
        *,
        robot_id: str,
        test_id: str,
        limb: str,
        quiet: bool = False,
    ) -> None:
        self.robot_id = robot_id
        self.test_id = test_id
        self.limb = limb
        self.quiet = quiet

        token = os.environ.get("INFLUXDB_TOKEN", "").strip()
        self.enabled = bool(token)
        self.url = os.environ.get("INFLUXDB_URL", "http://localhost:8181").rstrip("/")
        self.database = os.environ.get("INFLUXDB_DB", "robot_test").strip() or "robot_test"
        self.token = token
        self.sample_stride = max(1, _env_int("INFLUX_SAMPLE_STRIDE", 10))
        self.flush_interval_s = max(0.05, _env_float("INFLUX_FLUSH_INTERVAL_S", 0.2))
        self.flush_max_lines = max(1, _env_int("INFLUX_FLUSH_MAX_LINES", 40))

        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=20_000)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.enqueued_lines = 0
        self.posted_lines = 0
        self.dropped_lines = 0
        self.post_errors = 0

    def start(self) -> InfluxTelemetryWriter:
        if not self.enabled:
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._flush_loop, name="influx-telemetry", daemon=True)
        self._thread.start()
        if not self.quiet:
            print(
                f"influx sync: {self.url} db={self.database} "
                f"robot_id={self.robot_id} test_id={self.test_id} limb={self.limb} "
                f"stride={self.sample_stride} flush<={self.flush_interval_s:.2f}s/"
                f"{self.flush_max_lines} lines"
            )
        return self

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if not self.quiet:
            print(
                f"influx sync stopped: posted={self.posted_lines} "
                f"enqueued={self.enqueued_lines} dropped={self.dropped_lines} "
                f"errors={self.post_errors}"
            )

    def enqueue_raw(self, line: str) -> bool:
        """Enqueue one already encoded line-protocol point without sampling stride."""
        if not self.enabled:
            return False
        if "\n" in line or "\r" in line:
            raise ValueError("raw line protocol point must contain exactly one line")
        try:
            self._queue.put_nowait(line)
            self.enqueued_lines += 1
            return True
        except queue.Full:
            self.dropped_lines += 1
            return False

    def maybe_enqueue(
        self,
        entries: list[MotorEntry],
        samples: list[MotorSample],
        *,
        sample_index: int,
    ) -> None:
        """Enqueue joint points for every ``sample_stride``-th CSV sample."""
        if not self.enabled:
            return
        if sample_index % self.sample_stride != 0:
            return
        timestamp_ns = time.time_ns()
        try:
            lines = samples_to_line_protocol(
                entries,
                samples,
                robot_id=self.robot_id,
                test_id=self.test_id,
                limb=self.limb,
                timestamp_ns=timestamp_ns,
            )
        except Exception as exc:
            self.post_errors += 1
            if not self.quiet and self.post_errors <= 3:
                print(f"influx encode failed: {exc}", file=sys.stderr)
            return
        for line in lines:
            self.enqueue_raw(line)

    def _flush_loop(self) -> None:
        batch: list[str] = []
        last_flush = time.monotonic()
        while True:
            timeout = self.flush_interval_s
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None
                if batch:
                    self._post_batch(batch)
                    batch = []
                    last_flush = time.monotonic()
                if self._stop.is_set() and self._queue.empty():
                    break
                continue

            if item is None:
                if batch:
                    self._post_batch(batch)
                    batch = []
                if self._stop.is_set() and self._queue.empty():
                    break
                continue

            batch.append(item)
            due = (time.monotonic() - last_flush) >= self.flush_interval_s
            if len(batch) >= self.flush_max_lines or due:
                self._post_batch(batch)
                batch = []
                last_flush = time.monotonic()

        # Drain anything left after stop sentinel handling
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                batch.append(item)
        if batch:
            self._post_batch(batch)

    def _post_batch(self, lines: list[str]) -> None:
        if not lines:
            return
        endpoint = f"{self.url}/api/v3/write_lp?db={self.database}&precision=nanosecond"
        body = ("\n".join(lines) + "\n").encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "text/plain; charset=utf-8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5.0) as response:
                response.read()
            self.posted_lines += len(lines)
        except urllib.error.HTTPError as exc:
            self.post_errors += 1
            detail = exc.read().decode("utf-8", errors="replace")
            if not self.quiet and self.post_errors <= 5:
                print(f"influx write HTTP {exc.code}: {detail}", file=sys.stderr)
        except Exception as exc:
            self.post_errors += 1
            if not self.quiet and self.post_errors <= 5:
                print(f"influx write failed: {exc}", file=sys.stderr)


def load_influx_meta(*, limb: str | None = None) -> tuple[bool, str, str, str]:
    """Return (enabled, robot_id, test_id, limb) from environment / defaults."""
    token = os.environ.get("INFLUXDB_TOKEN", "").strip()
    robot_id = os.environ.get("ROBOT_ID", "RP1-001").strip() or "RP1-001"
    test_id = os.environ.get("TEST_ID", "").strip()
    if not test_id:
        test_id = time.strftime("reliab-%Y%m%d-%H%M%S")
    limb_name = (limb or os.environ.get("LIMB", "unknown")).strip() or "unknown"
    return bool(token), robot_id, test_id, limb_name


class MotorTelemetryRecorder:
    """Background CSV logger; reuse with control scripts on the same motor handles.

    The control loop should call ``refresh_motor_status()`` after MIT commands;
    this recorder reads cached feedback with ``query=False``.

    If ``INFLUXDB_TOKEN`` is set, samples are also queued to InfluxDB 3 in a
    separate flush thread (CSV-only when token is absent).
    """

    def __init__(
        self,
        motors: list[Any],
        entries: list[MotorEntry],
        output: Path | str,
        *,
        rate_hz: float = 50.0,
        query: bool = False,
        get_cmd_pos: Any = None,
        sample_provider: Any = None,
        print_status: bool = False,
        live_display: LiveBlockDisplay | None = None,
        quiet: bool = False,
        limb: str | None = None,
        robot_id: str | None = None,
        test_id: str | None = None,
        controller_heartbeat: ControllerHeartbeat | None = None,
        controller_stale_sec: float = 0.5,
        platform_required: bool = True,
    ) -> None:
        self.motors = motors
        self.entries = entries
        self.output = Path(output)
        self.rate_hz = float(rate_hz)
        self.query = bool(query)
        self.get_cmd_pos = get_cmd_pos
        self.sample_provider = sample_provider
        self.print_status = bool(print_status)
        self.live_display = live_display
        self.quiet = bool(quiet)
        enabled, env_robot, env_test, env_limb = load_influx_meta(limb=limb)
        self.limb = limb or env_limb
        self.robot_id = robot_id or env_robot
        self.test_id = test_id or env_test
        self.controller_heartbeat = controller_heartbeat
        self.controller_stale_sec = float(controller_stale_sec)
        self.platform_required = bool(platform_required)
        self.platform_sync_errors: list[str] = []
        if self.controller_stale_sec <= 0.0:
            raise ValueError("controller_stale_sec must be positive")
        self._influx_wanted = enabled
        self._fault_latch = FaultLatch()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._file: Any = None
        self._writer: csv.DictWriter | None = None
        self._influx: InfluxTelemetryWriter | None = None
        self._record_start_monotonic: float | None = None
        self._stopped_elapsed_s: float | None = None
        self._heartbeat_start_count = 0
        self._heartbeat_monitor_started = 0.0
        self._previous_error_ids = [0 for _ in entries]
        self.fault_count = 0
        self.row_count = 0
        self.controller_timeout = False
        self.blocked_reason: str | None = None

    def _update_platform_execution(self, **updates: Any) -> None:
        try:
            self._update_platform_execution_once(**updates)
        except Exception as exc:
            if self.platform_required:
                raise
            message = str(exc)
            self.platform_sync_errors.append(message)
            if not self.quiet:
                print(
                    f"warning: platform sync failed; local CSV remains active: {message}",
                    file=sys.stderr,
                )

    def _update_platform_execution_once(self, **updates: Any) -> None:
        base_url = os.environ.get("TEST_PLATFORM_API_URL", "").strip().rstrip("/")
        if not base_url:
            return
        if not self.test_id:
            raise RuntimeError("TEST_PLATFORM_API_URL 已配置，但 TEST_ID 为空")
        url = (
            f"{base_url}/api/executions/"
            f"{urllib.parse.quote(self.test_id, safe='')}"
        )
        body = json.dumps(updates).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="PATCH",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                if response.status != 200:
                    raise RuntimeError(f"execution update returned HTTP {response.status}")
        except Exception as exc:
            raise RuntimeError(
                f"PostgreSQL 执行台账更新失败 ({self.test_id}): {exc}"
            ) from exc

    def start(self) -> MotorTelemetryRecorder:
        self._update_platform_execution(
            status="running",
            last_data_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            telemetry_source="InfluxDB/joint_metrics",
        )
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.output.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._file, fieldnames=csv_fieldnames(self.entries))
        self._writer.writeheader()
        self._stop.clear()
        self._stopped_elapsed_s = None
        self.controller_timeout = False
        self.blocked_reason = None
        if self.controller_heartbeat is not None:
            self._heartbeat_start_count, _ = self.controller_heartbeat.snapshot()
        self._heartbeat_monitor_started = time.monotonic()
        if self._influx_wanted:
            self._influx = InfluxTelemetryWriter(
                robot_id=self.robot_id,
                test_id=self.test_id,
                limb=self.limb,
                quiet=self.quiet,
            ).start()
            self._record_start_monotonic = time.monotonic()
            self._enqueue_event("start", elapsed_s=0.0)
        elif not self.quiet:
            print("influx sync: skipped (set INFLUXDB_TOKEN to enable live Grafana feed)")
        if self._record_start_monotonic is None:
            self._record_start_monotonic = time.monotonic()
        self._previous_error_ids = [0 for _ in self.entries]
        self.fault_count = 0
        if not self.quiet:
            print(f"telemetry logging: {self.output} @ {self.rate_hz:.1f} Hz")
        self._thread = threading.Thread(target=self._run, name="motor-telemetry", daemon=True)
        self._thread.start()
        return self

    def stop(
        self,
        *,
        final_status: str | None = None,
        final_summary: str | None = None,
        joint_metrics: list[dict[str, Any]] | None = None,
    ) -> int:
        self.stop_sampling()
        self.finalize(
            final_status=final_status,
            final_summary=final_summary,
            joint_metrics=joint_metrics,
        )
        return self.row_count

    def stop_sampling(self) -> int:
        """Stop CSV sampling without waiting on network post-processing."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        if self._stopped_elapsed_s is None:
            self._stopped_elapsed_s = self._elapsed_s()
        if self._file is not None:
            self._file.close()
            self._file = None
        return self.row_count

    def finalize(
        self,
        *,
        final_status: str | None = None,
        final_summary: str | None = None,
        joint_metrics: list[dict[str, Any]] | None = None,
    ) -> None:
        """Flush remote sinks and update the platform after motors are safe."""
        self.stop_sampling()
        if self._influx is not None:
            duration_s = self._elapsed_s()
            self._enqueue_event(
                "stop",
                elapsed_s=duration_s,
                duration_s=duration_s,
                fault_count=self.fault_count,
            )
            self._influx.stop()
            self._influx = None
        duration_s = self._elapsed_s()
        updates: dict[str, Any] = dict(
            status=final_status or ("failed" if self.fault_count else "passed"),
            ended_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            last_data_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            duration_hours=duration_s / 3600,
            exception_count=self.fault_count,
            summary=final_summary or f"遥测采集完成，共 {self.row_count} 行",
        )
        if joint_metrics is not None:
            updates["joint_metrics"] = joint_metrics
        self._update_platform_execution(**updates)
        if not self.quiet:
            print(f"telemetry saved {self.row_count} row(s) to {self.output}")

    def _elapsed_s(self) -> float:
        if self._stopped_elapsed_s is not None:
            return self._stopped_elapsed_s
        if self._record_start_monotonic is None:
            return 0.0
        return max(0.0, time.monotonic() - self._record_start_monotonic)

    def _enqueue_event(
        self,
        event_type: str,
        *,
        elapsed_s: float,
        joint: str | None = None,
        error_id: int | None = None,
        duration_s: float | None = None,
        fault_count: int | None = None,
    ) -> None:
        if self._influx is None:
            return
        line = test_event_to_line_protocol(
            robot_id=self.robot_id,
            test_id=self.test_id,
            limb=self.limb,
            event_type=event_type,
            elapsed_s=elapsed_s,
            timestamp_ns=time.time_ns(),
            joint=joint,
            error_id=error_id,
            duration_s=duration_s,
            fault_count=fault_count,
        )
        self._influx.enqueue_raw(line)

    def _enqueue_fault_transitions(
        self,
        samples: list[MotorSample],
        *,
        elapsed_s: float,
    ) -> None:
        for index, (entry, item) in enumerate(zip(self.entries, samples)):
            error_id = int(item.error_id)
            previous = self._previous_error_ids[index]
            if error_id != 0 and error_id != previous:
                self._enqueue_event(
                    "fault",
                    elapsed_s=elapsed_s,
                    joint=entry.joint_name,
                    error_id=error_id,
                )
                self.fault_count += 1
            self._previous_error_ids[index] = error_id

    def __enter__(self) -> MotorTelemetryRecorder:
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _run(self) -> None:
        assert self._writer is not None
        period = 1.0 / self.rate_hz
        start = time.monotonic()
        sample_index = 0
        while not self._stop.is_set():
            loop_start = time.monotonic()
            if self.controller_heartbeat is not None:
                touch_count, _ = self.controller_heartbeat.snapshot()
                heartbeat_age = self.controller_heartbeat.age(loop_start)
                waiting_for_first_dispatch = touch_count <= self._heartbeat_start_count
                first_dispatch_wait = loop_start - self._heartbeat_monitor_started
                if waiting_for_first_dispatch and first_dispatch_wait <= self.controller_stale_sec:
                    time.sleep(min(period, self.controller_stale_sec - first_dispatch_wait))
                    continue
                if waiting_for_first_dispatch or heartbeat_age is None or heartbeat_age > self.controller_stale_sec:
                    age_text = "never touched" if heartbeat_age is None else f"{heartbeat_age:.3f}s old"
                    self.controller_timeout = True
                    self.blocked_reason = (
                        "controller_timeout: control heartbeat is "
                        f"{age_text} (limit {self.controller_stale_sec:.3f}s)"
                    )
                    self._enqueue_event(
                        "controller_timeout",
                        elapsed_s=self._elapsed_s(),
                    )
                    self._stop.set()
                    break
            if self.query:
                for motor in self.motors:
                    motor.refresh_motor_status()
            if self.sample_provider is not None:
                samples = list(self.sample_provider())
                if len(samples) != len(self.entries):
                    sleep_s = period - (time.monotonic() - loop_start)
                    if sleep_s > 0.0:
                        self._stop.wait(sleep_s)
                    continue
            else:
                cmd_pos = None
                if self.get_cmd_pos is not None:
                    cmd_pos = [float(value) for value in self.get_cmd_pos()]
                samples = read_motor_samples(self.motors, cmd_pos=cmd_pos)
            elapsed = loop_start - start
            self._writer.writerow(
                sample_row(
                    self.entries,
                    samples,
                    elapsed_s=elapsed,
                    sample=sample_index,
                )
            )
            if self._influx is not None:
                self._influx.maybe_enqueue(
                    self.entries,
                    samples,
                    sample_index=sample_index,
                )
                self._enqueue_fault_transitions(samples, elapsed_s=self._elapsed_s())
            if self.live_display is not None:
                self._fault_latch.update(self.entries, samples)
                self.live_display.update(
                    telemetry_live_lines(self.entries, samples, elapsed_s=elapsed),
                    footer=telemetry_live_footer(
                        rate_hz=self.rate_hz,
                        fault_latch=self._fault_latch,
                    ),
                )
            elif self.print_status and sample_index % 2 == 0:
                print_terminal_status(self.entries, samples, elapsed_s=elapsed)
            self.row_count += 1
            sample_index += 1
            sleep_s = period - (time.monotonic() - loop_start)
            if sleep_s > 0.0:
                time.sleep(sleep_s)


def _joint_names_from_sim_csv(fieldnames: list[str]) -> list[str]:
    suffix = "_actual_rad"
    names = []
    for field in fieldnames:
        if field.endswith(suffix):
            names.append(field[: -len(suffix)])
    return names


def load_sim_joint_csv(path: Path) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, tuple[float, float]]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{path} has no header row")
        rows = list(reader)
        fieldnames = list(reader.fieldnames)

    if not rows:
        return np.asarray([], dtype=np.float64), {}, {}

    joint_names = _joint_names_from_sim_csv(fieldnames)
    if not joint_names:
        raise ValueError(f"{path} contains no *_actual_rad columns")

    time_s = np.asarray([float(row["time_s"]) for row in rows], dtype=np.float64)
    series: dict[str, np.ndarray] = {}
    limits: dict[str, tuple[float, float]] = {}
    for name in joint_names:
        series[f"{name}_actual"] = np.asarray([float(row[f"{name}_actual_rad"]) for row in rows], dtype=np.float64)
        series[f"{name}_target"] = np.asarray([float(row[f"{name}_target_rad"]) for row in rows], dtype=np.float64)
        lo = float(rows[-1][f"{name}_limit_lo_rad"])
        hi = float(rows[-1][f"{name}_limit_hi_rad"])
        limits[name] = (lo, hi)
    return time_s, series, limits


def run_sim_live_plot(joint_csv: Path, refresh_hz: float) -> int:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("matplotlib is required for --sim-live: pip install matplotlib") from exc

    period = 1.0 / float(refresh_hz)
    joint_csv = joint_csv.expanduser().resolve()
    print(f"sim live plot: {joint_csv}")
    print(f"refresh: {refresh_hz:.3f} Hz; close plot window or Ctrl+C to exit")

    fig, ax = plt.subplots(figsize=(12, 6))
    plt.ion()
    fig.show()
    line_map: dict[str, object] = {}
    limit_artists: dict[str, tuple[object, object]] = {}

    try:
        while plt.fignum_exists(fig.number):
            if not joint_csv.exists():
                time.sleep(period)
                continue

            time_s, series, limits = load_sim_joint_csv(joint_csv)
            if time_s.size == 0:
                time.sleep(period)
                continue

            if not line_map:
                for name in limits:
                    (actual_line,) = ax.plot([], [], linewidth=1.4, label=f"{name} actual")
                    (target_line,) = ax.plot([], [], linewidth=0.9, linestyle="--", alpha=0.7, label=f"{name} target")
                    lo_line = ax.axhline(limits[name][0], color=actual_line.get_color(), linestyle=":", alpha=0.35)
                    hi_line = ax.axhline(limits[name][1], color=actual_line.get_color(), linestyle=":", alpha=0.35)
                    line_map[name] = (actual_line, target_line)
                    limit_artists[name] = (lo_line, hi_line)
                ax.set_xlabel("time (s)")
                ax.set_ylabel("joint angle (rad)")
                ax.set_title("Sim joint angles (live)")
                ax.grid(True, alpha=0.3)
                ax.legend(loc="best", fontsize=7, ncol=2)

            for name, (actual_line, target_line) in line_map.items():
                actual_line.set_data(time_s, series[f"{name}_actual"])
                target_line.set_data(time_s, series[f"{name}_target"])

            ax.relim()
            ax.autoscale_view()
            fig.canvas.draw_idle()
            fig.canvas.flush_events()
            plt.pause(period)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        plt.close(fig)
    return 0


def main() -> int:
    args = parse_args()

    if args.sim_live:
        if args.dry_run:
            print(f"sim live plot source: {args.joint_csv}")
            return 0
        return run_sim_live_plot(args.joint_csv, float(args.rate))

    limb = canonical_limb(args.limb)
    config = load_yaml_config(args.config)
    entries = limb_motor_entries(config, limb)
    fieldnames = csv_fieldnames(entries)

    motor_pos = None
    trajectory_time = None
    if args.trajectory is not None:
        motor_pos, _, traj_limb, trajectory_time = load_motor_position_npz(args.trajectory)
        if traj_limb is not None and canonical_limb(traj_limb) != limb:
            print(
                f"warning: trajectory limb {traj_limb!r} differs from --limb {limb!r}",
                file=sys.stderr,
            )
        if motor_pos.shape[1] != len(entries):
            print(
                f"trajectory motor count {motor_pos.shape[1]} != config motor count {len(entries)}",
                file=sys.stderr,
            )
            return 1

    print(f"config: {args.config}")
    print(f"limb: {limb}")
    print(f"output: {args.output}")
    print_motor_table(entries)
    print("CSV columns:", ", ".join(fieldnames))
    print(
        "note: get_motor_current() is motor output torque (Nm) on LRO; "
        "get_motor_temperature() is coil temperature in deg C"
    )

    if args.query:
        print("mode: active query (refresh_motor_status per motor each sample)")
        print("warning: do not run together with replay_limb_traj on the same CAN bus")
    else:
        print("mode: passive listen (no init/deinit, no MIT commands)")
        print("start replay_limb_traj.py in another terminal, then run this logger")

    if args.trajectory is not None:
        print(f"trajectory: {args.trajectory} (cmd_pos from replay target @ speed {args.speed:g}x)")
    else:
        print("cmd_pos: not available without --trajectory (passive listen cannot see other process MIT)")

    if not args.no_print:
        print(f"terminal status: every 2 samples (~{float(args.rate) * 0.5:.1f} Hz)")

    if args.dry_run:
        return 0

    add_motors_python_paths()
    try:
        import motors_py
    except Exception as exc:
        print(f"failed to import motors_py: {exc}", file=sys.stderr)
        print("run ./tools/build_motors_py.sh first", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    motors = []
    row_count = 0

    try:
        for entry in entries:
            print(f"listen motor idx={entry.index} id={entry.motor_id} iface={entry.interface}")
            motors.append(create_motor_driver(entry, motors_py))

        duration = float(args.duration)
        if duration > 0.0:
            print(f"logging at {float(args.rate):.3f} Hz for {duration:.3f}s")
        else:
            print(f"logging at {float(args.rate):.3f} Hz; press Ctrl+C to stop")

        if args.trajectory is not None:
            input("Press Enter when replay starts (P) to sync command position time base... ")

        with args.output.open("w", encoding="utf-8", newline="") as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=fieldnames)
            writer.writeheader()
            row_count = collect_and_log(
                motors,
                entries,
                writer,
                rate_hz=float(args.rate),
                duration_s=duration,
                query=bool(args.query),
                motor_pos=motor_pos,
                trajectory_time=trajectory_time,
                playback_speed=float(args.speed),
                print_status=not args.no_print,
            )
    except KeyboardInterrupt:
        print("\ninterrupted")
    except Exception as exc:
        print(f"log failed: {exc}", file=sys.stderr)
        return 1
    finally:
        # Passive mode: never call deinit_motor(); replay process owns motor enable state.
        pass

    print(f"saved {row_count} row(s) to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
