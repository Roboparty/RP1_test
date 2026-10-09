"""Disk-backed telemetry sorting and sampling for finalized run ingestion."""

from __future__ import annotations

import csv
import math
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

METRIC_COLUMNS = (
    "cmd_pos_rad",
    "pos_rad",
    "tracking_error_rad",
    "spd_rad_s",
    "torque_nm",
    "temp_c",
)


class TelemetrySpool:
    """Keep CSV rows and exact stride statistics on disk, not in Python lists.

    Equal timestamps retain CSV order. The SQLite cache and sort workspace are
    bounded independently of row count; callers consume at most one point chunk.
    """

    def __init__(self, path: Path) -> None:
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA cache_size=-2048")
        self.db.execute("PRAGMA temp_store=FILE")
        self.db.execute(
            "CREATE TABLE telemetry (ordinal INTEGER PRIMARY KEY, motor INTEGER, "
            "time REAL, cmd_pos_rad REAL, pos_rad REAL, tracking_error_rad REAL, "
            "spd_rad_s REAL, torque_nm REAL, temp_c REAL)"
        )
        self.db.execute(
            "CREATE TABLE sampled (ordinal INTEGER PRIMARY KEY, time REAL, "
            "cmd_pos_rad REAL, pos_rad REAL, tracking_error_rad REAL, "
            "spd_rad_s REAL, torque_nm REAL, temp_c REAL)"
        )
        self.db.execute("CREATE TABLE deltas (value REAL NOT NULL)")
        self.row_count = 0

    def close(self) -> None:
        self.db.close()

    def load(self, path: Path, parse_value: Callable[[str, str], Any]) -> None:
        with path.open("r", encoding="utf-8", newline="") as handle, self.db:
            reader = csv.DictReader(handle)
            if not {"time_s", "motor_index"}.issubset(reader.fieldnames or []):
                raise ValueError("telemetry CSV requires time_s and motor_index columns")
            for ordinal, raw in enumerate(reader):
                try:
                    if None in raw or any(value is None for value in raw.values()):
                        raise ValueError("column count differs from the header")
                    row = {name: parse_value(name, value) for name, value in raw.items()}
                except (ValueError, TypeError) as exc:
                    raise ValueError(
                        f"{path}:{reader.line_num}: invalid telemetry row: {exc}"
                    ) from exc
                actual, target = row.get("pos_rad"), row.get("cmd_pos_rad")
                error = None
                if isinstance(actual, (int, float)) and isinstance(target, (int, float)):
                    difference = actual - target
                    if math.isfinite(difference):
                        error = difference
                row["tracking_error_rad"] = error
                self.db.execute(
                    "INSERT INTO telemetry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        ordinal,
                        row.get("motor_index"),
                        row.get("time_s"),
                        *(row.get(column) for column in METRIC_COLUMNS),
                    ),
                )
                self.row_count += 1
            self.db.execute("CREATE INDEX telemetry_order ON telemetry(motor, time, ordinal)")

    def sample_motor(self, motor_index: int, requested_stride: int) -> int:
        """Apply the original exact median interval and uniform row stride."""
        count, timed = self.db.execute(
            "SELECT COUNT(*), COUNT(time) FROM telemetry WHERE motor=?", (motor_index,)
        ).fetchone()
        if not count:
            raise ValueError(f"telemetry has no rows for motor_index={motor_index}")
        if count != timed:
            raise ValueError(f"motor_index={motor_index} has missing or nonfinite time_s")
        with self.db:
            self.db.execute("DELETE FROM deltas")
            self.db.execute("DELETE FROM sampled")
            previous = None
            delta_count = 0
            for (time_s,) in self.db.execute(
                "SELECT time FROM telemetry WHERE motor=? ORDER BY time, ordinal", (motor_index,)
            ):
                if previous is not None:
                    delta = time_s - previous
                    if delta > 0:
                        self.db.execute("INSERT INTO deltas VALUES (?)", (delta,))
                        delta_count += 1
                previous = time_s
            stride = max(1, requested_stride)
            if delta_count:
                # SQLite's sorter spills to disk. Only the middle one or two
                # values enter Python, including for arbitrarily unsorted CSVs.
                middle = self.db.execute(
                    "SELECT value FROM deltas ORDER BY value LIMIT ? OFFSET ?",
                    (2 if delta_count % 2 == 0 else 1, (delta_count - 1) // 2),
                ).fetchall()
                interval = sum(item[0] for item in middle) / len(middle)
                stride = max(stride, int(math.ceil(0.010 / interval)))
            columns = ", ".join(METRIC_COLUMNS)
            for index, row in enumerate(
                self.db.execute(
                    f"SELECT ordinal, time, {columns} FROM telemetry "
                    "WHERE motor=? ORDER BY time, ordinal",
                    (motor_index,),
                )
            ):
                if index % stride == 0:
                    self.db.execute("INSERT INTO sampled VALUES (?, ?, ?, ?, ?, ?, ?, ?)", row)
        return stride

    def metric_chunks(self, column: str, size: int) -> Iterator[list[dict[str, Any]]]:
        if column not in METRIC_COLUMNS:
            raise ValueError(f"unsupported telemetry column: {column}")
        cursor = self.db.execute(
            f"SELECT time, {column} FROM sampled WHERE {column} IS NOT NULL ORDER BY time, ordinal"
        )
        while rows := cursor.fetchmany(size):
            yield [{"time_s": time_s, column: value} for time_s, value in rows]
