"""Bounded CSV conversion and viewport queries. Originals are never rewritten."""
from contextlib import contextmanager
import csv
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Callable

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from app.core.errors import ApiError
from app.services.archive_storage import ArchiveStorage, fsync_directory, sha256_file


PROCESSOR_VERSION = "rp1-csv-analysis/1"
MAX_COLUMNS = 512
MAX_SERIES = 32
MAX_QUERY_POINTS = 20000
SUFFIX_UNITS = {
    "tracking_error_rad": "rad", "cmd_pos_rad": "rad", "pos_rad": "rad",
    "spd_rad_s": "rad/s", "vel_rad_s": "rad/s", "torque_nm": "N·m",
    "temp_c": "°C", "error_id": "", "bus_voltage_v": "V", "bus_current_a": "A",
    "current_a": "A", "voltage_v": "V",
}


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def finite(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) and abs(result) <= 1e100 else None
    except (ValueError, TypeError, OverflowError):
        return None


def catalog_item(key: str):
    for suffix, unit in SUFFIX_UNITS.items():
        if key.endswith("_" + suffix):
            return {"key": key, "subject": key[: -len(suffix)-1], "metric": suffix, "unit": unit}
    return {"key": key, "subject": "execution", "metric": key, "unit": ""}


@dataclass
class Statistic:
    count: int = 0
    missing: int = 0
    mean: float = 0.0
    sum_squares: float = 0.0
    minimum: float | None = None
    maximum: float | None = None
    first: float | None = None
    last: float | None = None
    faults: int = 0
    fault_active: bool = False

    def add(self, value, is_fault=False):
        if value is None:
            self.missing += 1
            return
        self.count += 1
        self.mean += (value - self.mean) / self.count
        self.sum_squares += value * value
        self.minimum = value if self.minimum is None else min(self.minimum, value)
        self.maximum = value if self.maximum is None else max(self.maximum, value)
        if self.first is None:
            self.first = value
        self.last = value
        if is_fault:
            active = value != 0
            self.faults += int(active and not self.fault_active)
            self.fault_active = active

    def export(self, key):
        return {"key": key, "count": self.count, "missing": self.missing,
                "min": self.minimum, "max": self.maximum,
                "mean": self.mean if self.count else None,
                "rms": math.sqrt(self.sum_squares / self.count) if self.count else None,
                "first": self.first, "last": self.last,
                "rise": self.maximum-self.first if self.count else None,
                "end_rise": self.last-self.first if self.count else None,
                "fault_count": self.faults if key.endswith("_error_id") else None}


@contextmanager
def connection(directory: Path, memory_mb: int = 512):
    with tempfile.TemporaryDirectory(prefix=".query-", dir=directory) as temporary:
        conn = duckdb.connect(":memory:", config={"memory_limit": f"{memory_mb}MB", "threads": "2", "temp_directory": temporary, "max_temp_directory_size": "10GB", "enable_external_access": "true"})
        try:
            # No user-provided SQL, paths, identifiers, extensions or URLs are used.
            yield conn
        finally:
            conn.close()


def bounded_lines(handle, limit=2 * 1024**2):
    while line := handle.readline(limit + 1):
        if len(line) > limit:
            raise ValueError("CSV 单行过长，已保留原件；请检查格式")
        yield line


class CurveProcessor:
    def __init__(self, storage: ArchiveStorage, memory_mb=512):
        self.storage = storage
        self.memory_mb = memory_mb

    def build(self, archive: dict, heartbeat: Callable[[], None] = lambda: None) -> dict:
        execution_uuid = archive["execution_uuid"]
        directory = self.storage.directory(execution_uuid)
        raw = self.storage.path(execution_uuid, "raw.csv")
        if sha256_file(raw,heartbeat) != archive["sha256"]:
            raise ValueError("原始 CSV 哈希不符，拒绝生成查询副本")
        csv.field_size_limit(4096)
        temporary = directory / "telemetry.parquet.tmp"
        if temporary.is_symlink():
            raise ValueError("派生文件路径不可是符号链接")
        rows_seen = invalid_time = non_monotonic = 0
        previous = None
        first_time = last_time = None
        max_gap = 0.0
        stages: set[str] = set()
        cycles: set[str] = set()
        with raw.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(bounded_lines(handle))
            fields = next(reader, [])
            if len(fields) < 2 or len(fields) > MAX_COLUMNS or len(fields) != len(set(fields)):
                raise ValueError("CSV 必须包含唯一表头，且总列数介于 2 至 512")
            if any(not field or len(field) > 120 or any(ord(c) < 32 for c in field) or field.startswith("_") for field in fields):
                raise ValueError("CSV 表头含非法或保留字段")
            time_key = next((key for key in ("time_s", "elapsed_s", "elapsed_seconds") if key in fields), None)
            if time_key is None:
                raise ValueError("CSV 缺少 time_s、elapsed_s 或 elapsed_seconds 秒时间列；原件已保存")
            stage_key = next((key for key in ("stage", "stage_code") if key in fields), None)
            cycle_key = next((key for key in ("cycle", "cycle_no", "cycle_index") if key in fields), None)
            metric_keys = [key for key in fields if key not in {time_key, stage_key, cycle_key, "sample"}]
            tracking = {key[:-12]+"_tracking_error_rad": (key, key[:-12]+"_pos_rad") for key in metric_keys if key.endswith("_cmd_pos_rad") and key[:-12]+"_pos_rad" in fields}
            # Do not silently replace an explicitly recorded tracking error.
            tracking = {key: sources for key, sources in tracking.items() if key not in fields}
            metric_keys.extend(tracking)
            if len(metric_keys) > MAX_COLUMNS:
                raise ValueError("派生曲线总列数超限")
            schema = pa.schema([pa.field("_row", pa.int64()), pa.field("time_s", pa.float64()), pa.field("_stage", pa.string()), pa.field("_cycle", pa.string())] + [pa.field(key, pa.float64()) for key in metric_keys])
            stats = {key: Statistic() for key in metric_keys}
            buffer = {key: [] for key in schema.names}
            batch_size = max(128, min(8192, 131072 // max(1, len(metric_keys))))
            indexes = {key: index for index, key in enumerate(fields)}
            with pq.ParquetWriter(temporary, schema, compression="zstd", write_page_checksum=True) as writer:
                def flush():
                    if buffer["time_s"]:
                        writer.write_table(pa.Table.from_pydict(buffer, schema=schema), row_group_size=batch_size)
                        for values in buffer.values():
                            values.clear()
                        heartbeat()

                for row in reader:
                    rows_seen += 1
                    if len(row) != len(fields):
                        raise ValueError(f"CSV 第 {rows_seen+1} 行列数与表头不一致")
                    timestamp = finite(row[indexes[time_key]])
                    if timestamp is None or timestamp < 0 or timestamp > 315360000:
                        invalid_time += 1
                        continue
                    if previous is not None:
                        non_monotonic += int(timestamp <= previous)
                        max_gap = max(max_gap, timestamp-previous)
                    previous = timestamp
                    first_time = timestamp if first_time is None else min(first_time, timestamp)
                    last_time = timestamp if last_time is None else max(last_time, timestamp)
                    stage = row[indexes[stage_key]].strip() if stage_key else ""
                    cycle = row[indexes[cycle_key]].strip() if cycle_key else ""
                    if len(stage) > 100 or len(cycle) > 100:
                        raise ValueError("CSV 阶段或循环标识过长")
                    if stage:
                        stages.add(stage)
                    if cycle:
                        cycles.add(cycle)
                    if len(stages) > 1000 or len(cycles) > 10000:
                        raise ValueError("CSV 阶段或循环种类超限")
                    values = {key: finite(row[indexes[key]]) for key in metric_keys if key not in tracking}
                    for key, (command, actual) in tracking.items():
                        values[key] = values[command]-values[actual] if values[command] is not None and values[actual] is not None else None
                    buffer["_row"].append(rows_seen)
                    buffer["time_s"].append(timestamp)
                    buffer["_stage"].append(stage)
                    buffer["_cycle"].append(cycle)
                    for key, value in values.items():
                        stats[key].add(value, key.endswith("_error_id"))
                        buffer[key].append(value)
                    if len(buffer["time_s"]) >= batch_size:
                        flush()
                flush()
        if first_time is None or not metric_keys:
            temporary.unlink(missing_ok=True)
            raise ValueError("CSV 没有有效时间点或可绘制参数；原件已保留")
        destination = self.storage.path(execution_uuid, "telemetry.parquet")
        with temporary.open("rb") as file:
            os.fsync(file.fileno())
        os.replace(temporary, destination)
        quality = {"row_count": rows_seen, "valid_time_rows": rows_seen-invalid_time, "invalid_time_rows": invalid_time,
                   "non_monotonic": non_monotonic, "max_gap_seconds": max_gap}
        rules = archive["acceptance"]
        rate = rules.get("record_rate_hz")
        active = archive["facts"].get("active_seconds")
        expected = math.floor(active * rate) + 1 if rate and active is not None else None
        quality["expected_sample_count"] = expected
        quality["sample_completeness"] = min(1.0, (rows_seen-invalid_time)/expected) if expected else None
        catalog = [catalog_item(key) for key in metric_keys]
        analysis = self.analyze(archive["facts"], rules, quality, stats)
        gap_limit = rules.get("max_gap_seconds")
        with connection(directory, self.memory_mb) as conn:
            conn.read_parquet(str(destination)).create_view("samples")
            if gap_limit is None:
                median_step = conn.execute("SELECT median(delta) FROM (SELECT time_s-lag(time_s) OVER (ORDER BY time_s,_row) delta FROM samples) WHERE delta>0").fetchone()[0]
                gap_limit = 5 * median_step if median_step is not None else 0
            gaps = conn.execute("SELECT previous,time_s FROM (SELECT time_s,lag(time_s) OVER (ORDER BY time_s,_row) previous FROM samples) WHERE time_s-previous>? ORDER BY time_s LIMIT 501", [gap_limit]).fetchall()
            pyramids = self.build_pyramids(conn, archive, catalog, first_time, last_time, heartbeat)
        manifest = {"version": PROCESSOR_VERSION, "original_sha256": archive["sha256"], "derived_sha256": sha256_file(destination),
                    "catalog": catalog, "time_range": {"start": first_time, "end": last_time}, "row_count": rows_seen-invalid_time,
                    "stages": sorted(stages), "cycles": sorted(cycles), "gap_threshold_seconds": gap_limit,
                    "gaps": [{"start": start, "end": end} for start, end in gaps[:500]], "gaps_truncated": len(gaps)>500,
                    "quality_status": "INVALID" if invalid_time or non_monotonic else "PARTIAL" if any(s.missing for s in stats.values()) or (quality["sample_completeness"] is not None and quality["sample_completeness"]<.999) or max_gap>gap_limit else "VALID",
                    "analysis": analysis, "pyramids": pyramids}
        encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > 2 * 1024**2:
            raise ValueError("分析目录超出受限元数据大小")
        manifest_path = self.storage.path(execution_uuid, "manifest.json")
        temporary_manifest = manifest_path.with_suffix(".tmp")
        with temporary_manifest.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_manifest, manifest_path)
        fsync_directory(directory)
        return manifest

    @staticmethod
    def analyze(facts, acceptance, quality, stats, *, honor_recorded_verdict=True):
        result = []
        def rule(name, actual, limit, unit="", minimum=False):
            verdict = "unconfigured" if limit is None or actual is None else "passed" if (actual>=limit if minimum else actual<=limit) else "failed"
            result.append({"name": name, "actual": actual, "limit": limit, "unit": unit, "result": verdict})
        rule("required_active_seconds", facts["active_seconds"], acceptance.get("required_active_seconds"), "s", True)
        rule("required_cycles", facts["completed_cycles"], acceptance.get("required_cycles"), "cycles", True)
        rule("sample_completeness", quality["sample_completeness"], acceptance.get("min_sample_completeness"), "ratio", True)
        rule("max_gap_seconds", quality["max_gap_seconds"], acceptance.get("max_gap_seconds"), "s")
        rule("invalid_time_rows", quality["invalid_time_rows"], 0)
        rule("non_monotonic_time_rows", quality["non_monotonic"], 0)
        rule("missing_numeric_samples", sum(stat.missing for stat in stats.values()), 0)
        rule("execution_completed", None if facts["machine_status"]=="unknown" else 1 if facts["machine_status"]=="completed" else 0, 1, minimum=True)
        for key, stat in stats.items():
            if key.endswith("_temp_c"):
                rule(key+".rise", stat.maximum-stat.first if stat.count else None, acceptance.get("max_temperature_rise_c"), "°C")
            elif key.endswith("_tracking_error_rad"):
                rule(key+".rms", math.sqrt(stat.sum_squares/stat.count) if stat.count else None, acceptance.get("max_tracking_rms_rad"), "rad")
            elif key.endswith("_torque_nm"):
                peak = max(abs(stat.minimum), abs(stat.maximum)) if stat.count else None
                rule(key+".peak_abs", peak, acceptance.get("max_torque_abs_nm"), "N·m")
            elif key.endswith("_error_id"):
                rule(key+".faults", stat.faults if stat.count else None, 0)
        for suffix, threshold, unit in (("_temp_c","max_temperature_rise_c","°C"),
                                       ("_tracking_error_rad","max_tracking_rms_rad","rad"),
                                       ("_torque_nm","max_torque_abs_nm","N·m")):
            if acceptance.get(threshold) is not None and not any(key.endswith(suffix) for key in stats):
                rule(threshold+".missing_measurement",None,acceptance[threshold],unit)
        failures = [r for r in result if r["result"]=="failed"]
        product_failure = facts["machine_status"]=="failed" or (honor_recorded_verdict and facts["automatic_verdict"]=="failed") or any(r["name"].endswith((".rise", ".rms", ".peak_abs", ".faults")) for r in failures)
        missing_facts = [key for key in ("active_seconds", "completed_cycles", "exception_count") if facts.get(key) is None]
        if facts.get("machine_status") == "unknown":
            missing_facts.append("machine_status")
        verdict = "failed" if product_failure else "blocked" if failures or missing_facts else "unconfigured" if acceptance.get("criteria_version", "UNCONFIGURED")=="UNCONFIGURED" or any(r["result"]=="unconfigured" for r in result) else "passed"
        return {"version": PROCESSOR_VERSION, "criteria_version": acceptance.get("criteria_version", "UNCONFIGURED"), "verdict": verdict,
                "missing_acquisition_facts": missing_facts, "rules": result, "metrics": [stat.export(key) for key, stat in stats.items()], "quality": quality, "timing": facts}

    def build_pyramids(self, conn, archive, catalog, start, end, heartbeat):
        if end <= start:
            return []
        # Bound each level's total size even for very wide CSVs.
        base_bins = max(32, min(4096, 131072 // len(catalog)))
        result = []
        schema = pa.schema([("key", pa.string()), ("t", pa.float64()), ("y", pa.float64()), ("row_id", pa.int64())])
        for level in range(4):
            bins = max(1, base_bins // 4**level)
            path = self.storage.path(archive["execution_uuid"], f"pyramid-{level}.parquet")
            temporary = path.with_suffix(".tmp")
            if temporary.is_symlink():
                raise ValueError("派生文件路径不可是符号链接")
            with pq.ParquetWriter(temporary, schema, compression="zstd", write_page_checksum=True) as writer:
                for item in catalog:
                    key = item["key"]
                    query = minmax_query(quoted(key), "samples", bins)
                    rows = conn.execute(query, [start, end, start, start, end]).fetchall()
                    if rows:
                        writer.write_table(pa.Table.from_pydict({"key": [key]*len(rows), "t": [r[0] for r in rows], "y": [r[1] for r in rows], "row_id": [r[2] for r in rows]}, schema=schema))
                    heartbeat()
            with temporary.open("rb") as handle:
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            result.append({"level": level, "bins": bins, "sha256": sha256_file(path)})
        return result


def minmax_query(column: str, source: str, bins: int) -> str:
    # Select original rows, retaining peak/trough and explicit missing values.
    return f"""WITH filtered AS (
        SELECT time_s,{column} AS value,_row,
          least({bins-1},greatest(0,floor((time_s-?)/greatest(?-?,1e-12)*{bins}))) AS bucket
        FROM {source} WHERE time_s BETWEEN ? AND ?
    ), selected AS (
        SELECT unnest([arg_min(_row,time_s),arg_max(_row,time_s),
            arg_min(_row,value),arg_max(_row,value),
            min(_row) FILTER(WHERE value IS NULL),max(_row) FILTER(WHERE value IS NULL)]) AS row_id
        FROM filtered GROUP BY bucket
    ) SELECT DISTINCT f.time_s,f.value,f._row FROM filtered f JOIN selected s ON f._row=s.row_id ORDER BY f.time_s,f._row"""


class CurveQueryService:
    def __init__(self, storage: ArchiveStorage, memory_mb=512):
        self.storage = storage
        self.memory_mb = memory_mb

    def query(self, archive, metrics, subjects, start, end, max_points, stage=None, cycle=None):
        if archive["processing_state"] != "READY":
            raise ApiError(409, "archive_not_ready", "原始文件正在校验分析或处理失败，请查看归档状态。")
        manifest = archive["manifest"]
        catalog = manifest["catalog"]
        if len(metrics)>32 or len(subjects)>32:
            raise ApiError(422, "too_many_series", "单次参数或关节选择最多 32 项。")
        available_metrics = {item["metric"] for item in catalog} | {item["key"] for item in catalog}
        if set(metrics)-available_metrics or set(subjects)-{item["subject"] for item in catalog}:
            raise ApiError(422, "unknown_curve", "所选参数或关节不在该归档目录中。")
        chosen = [item for item in catalog if (not metrics or item["metric"] in metrics or item["key"] in metrics) and (not subjects or item["subject"] in subjects)]
        if not metrics:
            chosen = chosen[:1]
        if not chosen or len(chosen)>MAX_SERIES:
            raise ApiError(422, "invalid_series_count", "请选择 1 至 32 条曲线。")
        if stage is not None and stage not in manifest["stages"] or cycle is not None and cycle not in manifest["cycles"]:
            raise ApiError(422, "unknown_stage_cycle", "该归档没有所选阶段或循环数据。")
        lower, upper = manifest["time_range"]["start"], manifest["time_range"]["end"]
        start = lower if start is None else start
        end = upper if end is None else end
        if not math.isfinite(start) or not math.isfinite(end) or end<start:
            raise ApiError(422, "invalid_time_range", "曲线时间范围无效。")
        max_points = max(50, min(MAX_QUERY_POINTS, max_points))
        budget = max_points // len(chosen)
        if budget < 8:
            raise ApiError(422, "point_budget", "曲线过多，请提高显示点数或减少曲线数量。")
        directory = self.storage.directory(archive["execution_uuid"])
        path = self.storage.path(archive["execution_uuid"], "telemetry.parquet")
        if not path.is_file():
            raise ApiError(409, "derived_file_missing", "曲线查询副本缺失，请由管理员重新处理归档。")
        filters = "time_s BETWEEN ? AND ?"
        filter_parameters = [start, end]
        if stage is not None:
            filters += " AND _stage=?"
            filter_parameters.append(stage)
        if cycle is not None:
            filters += " AND _cycle=?"
            filter_parameters.append(cycle)
        series = []
        try:
            with connection(directory, self.memory_mb) as conn:
                conn.read_parquet(str(path)).create_view("samples")
                # Validated categories are SQL-escaped. No full viewport materialization:
                # Parquet projects only selected columns and pushes time filters down.
                category_filter = "true"
                if stage is not None:
                    category_filter += " AND _stage='"+stage.replace("'", "''")+"'"
                if cycle is not None:
                    category_filter += " AND _cycle='"+cycle.replace("'", "''")+"'"
                conn.execute(f"CREATE VIEW viewport AS SELECT * FROM samples WHERE {category_filter}")
                count = manifest["row_count"] if start<=lower and end>=upper and stage is None and cycle is None else conn.execute(f"SELECT count(*) FROM samples WHERE {filters}",filter_parameters).fetchone()[0]
                gap_bins = max(1,budget//8)
                gap_points = conn.execute(f"""WITH times AS (
                    SELECT time_s,lag(time_s) OVER(ORDER BY time_s,_row) previous FROM viewport WHERE time_s BETWEEN ? AND ?
                ), gaps AS (
                    SELECT (time_s+previous)/2 midpoint,
                      least({gap_bins-1},greatest(0,floor((time_s-?)/greatest(?-?,1e-12)*{gap_bins}))) bucket
                    FROM times WHERE time_s-previous>?
                ) SELECT min(midpoint) FROM gaps GROUP BY bucket ORDER BY min(midpoint)""",
                    [start,end,start,end,start,manifest.get("gap_threshold_seconds",0)]).fetchall()
                for item in chosen:
                    column = quoted(item["key"])
                    if count<=budget//2:
                        rows = conn.execute(f"SELECT time_s,{column},_row FROM viewport WHERE time_s BETWEEN ? AND ? ORDER BY time_s,_row",[start,end]).fetchall()
                        downsampled = False
                    else:
                        bins = max(1, budget//8)
                        # Full-range views can use precomputed immutable min/max.
                        pyramid = next((p for p in manifest.get("pyramids", []) if p["bins"]*7<=budget), None) if start<=lower and end>=upper and stage is None and cycle is None else None
                        if pyramid:
                            pyramid_path = self.storage.path(archive["execution_uuid"], f"pyramid-{pyramid['level']}.parquet")
                            rows = conn.execute("SELECT t,y,row_id FROM read_parquet(?) WHERE key=? AND t BETWEEN ? AND ? ORDER BY t,row_id", [str(pyramid_path),item["key"],start,end]).fetchall()
                        else:
                            rows = conn.execute(minmax_query(column,"viewport",bins), [start,end,start,start,end]).fetchall()
                        downsampled = True
                    # Null markers at genuine gaps prevent misleading connecting lines.
                    points = [[float(t), float(value) if value is not None else None] for t,value,_ in rows]
                    remaining = budget-len(points)
                    for (midpoint,) in gap_points[:max(0,remaining)]:
                        points.append([float(midpoint),None])
                    points.sort(key=lambda point: point[0])
                    series.append({**item,"points": points,"sample_count": count,"returned_points": len(points),"downsampled": downsampled})
        except (duckdb.Error, OSError) as exc:
            raise ApiError(409, "curve_query_failed", "查询副本不可读或资源不足，请缩小范围或重新处理归档。") from exc
        return {"archive_id": archive["id"], "execution_uuid": archive["execution_uuid"], "archive_code": archive["archive_code"],
                "range": {"start": start,"end": end},"time_range": manifest["time_range"],"catalog": catalog,
                "series": series,"gaps": [g for g in manifest.get("gaps",[]) if g["end"]>start and g["start"]<end],
                "gaps_truncated": manifest.get("gaps_truncated",False),"resolution": "minmax" if any(s["downsampled"] for s in series) else "raw"}
