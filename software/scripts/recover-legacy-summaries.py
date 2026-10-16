#!/usr/bin/env python3
"""Import bounded local historical summaries into owner-written, unreviewed staging.

No formal execution, metric, event, runtime, or approval record is created here.
Use an isolated restored database first. Credentials stay inside Docker/psql.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "database/sql/028_legacy_recovery_staging.sql"
NUMERIC = (
    "mean_abs_torque_nm", "peak_abs_torque_nm", "temp_start_c", "temp_end_c",
    "temp_rise_c", "temp_max_c", "tracking_rms_rad", "bus_voltage_mean_v",
    "bus_voltage_min_v", "bus_voltage_max_v", "bus_current_mean_abs_a",
    "bus_current_peak_abs_a",
)
FORMAL_TABLES = (
    "test.test_execution", "test.execution_result", "test.runtime_interval",
    "test.test_event", "health.metric_observation", "health.metric_series",
    "reliability.exposure_assessment", "catalog.metric_definition",
    "catalog.metric_version", "catalog.telemetry_subject",
)
CONTEXT_SQL = """
SELECT coalesce(json_agg(x ORDER BY source_execution_key),'[]'::json) FROM (
 SELECT e.id,e.source_execution_key,e.normalized_started_at,e.normalized_ended_at,
        a.asset_code,st.station_code,tc.case_code
 FROM test.test_execution e JOIN test.asset a ON a.id=e.asset_id
 JOIN test.station st ON st.id=e.station_id
 JOIN catalog.test_case_version v ON v.id=e.test_case_version_id
 JOIN catalog.test_case tc ON tc.id=v.test_case_id
 WHERE e.source_execution_key IS NOT NULL
) x
"""
INFLUX_AGGREGATE_SQL = """SELECT test_id,joint,robot_id,limb,
 min(time) AS first_at,max(time) AS last_at,count(*) AS sample_count,
 avg(abs(torque_nm)) AS mean_abs_torque_nm,max(abs(torque_nm)) AS peak_abs_torque_nm,
 first_value(temp_c ORDER BY time ASC) AS temp_start_c,
 first_value(temp_c ORDER BY time DESC) AS temp_end_c,max(temp_c) AS temp_max_c,
 sqrt(avg((pos_rad-cmd_pos_rad)*(pos_rad-cmd_pos_rad))) AS tracking_rms_rad,
 avg(bus_voltage_v) AS bus_voltage_mean_v,min(bus_voltage_v) AS bus_voltage_min_v,
 max(bus_voltage_v) AS bus_voltage_max_v,
 avg(abs(bus_current_a)) AS bus_current_mean_abs_a,
 max(abs(bus_current_a)) AS bus_current_peak_abs_a,
 sum(CASE WHEN error_id <> 0 THEN 1 ELSE 0 END) AS error_sample_count,
 count(temp_c) AS temp_count,count(torque_nm) AS torque_count,
 count(pos_rad-cmd_pos_rad) AS tracking_count,count(bus_voltage_v) AS voltage_count,
 count(bus_current_a) AS current_count
 FROM joint_metrics GROUP BY test_id,joint,robot_id,limb
 ORDER BY test_id,joint,robot_id,limb"""


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def private_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
    path.chmod(0o600)


def literal(value):
    # Standard SQL quoting, never shell interpolation or executable dollar quotes.
    return "'" + str(value).replace("'", "''") + "'"


def psql(container, sql, readonly=False):
    info = json.loads(subprocess.check_output(["docker", "inspect", container]))[0]
    env = dict(item.split("=", 1) for item in info["Config"]["Env"] if "=" in item)
    args = ["docker", "exec", "-i"]
    if readonly:
        args += ["-e", "PGOPTIONS=-c default_transaction_read_only=on"]
    args += [container, "psql", "-X", "-qAt", "-U", env["POSTGRES_USER"],
             "-d", env["POSTGRES_DB"], "-v", "ON_ERROR_STOP=1"]
    result = subprocess.run(args, input=sql, text=True, capture_output=True)
    if result.returncode:
        # psql may echo SQL containing source data on failure: keep full diagnostics private.
        raise RuntimeError(result.stderr[-2000:])
    return result.stdout.strip()


def utc(value, influx=False):
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        if not influx:
            raise ValueError("PostgreSQL timestamp has no timezone")
        dt = dt.replace(tzinfo=timezone.utc)  # Influx timestamps are UTC nanoseconds.
    return dt.astimezone(timezone.utc).isoformat()


def proposed_subject(test_id, joint):
    part = re.match(r"(?:REL|PERF|FAT|ENV)-([A-Z]+)-", test_id)
    if not part or part[1] not in {"SARM", "SLEG", "UPPER", "LOWER", "SYS"}:
        return None
    match = re.fullmatch(r"(?:(left|right)_)?([a-z]+(?:_[a-z]+)?)_joint", joint)
    if not match:
        return None
    side, axis = match.groups()
    if axis in {"elbow", "knee"}:
        axis += "_pitch"
    code = part[1] + "-" + axis.upper().replace("_", "-")
    if side and part[1] not in {"SARM", "SLEG"}:
        code += "-" + side[0].upper()
    return code


def checked_summary(row):
    for name in NUMERIC:
        value = row.get(name)
        if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
            raise ValueError(f"Non-finite summary {name}")
    if row["sample_count"] <= 0:
        raise ValueError("Empty summary")
    for name in ("mean_abs_torque_nm", "peak_abs_torque_nm", "tracking_rms_rad",
                 "bus_current_mean_abs_a", "bus_current_peak_abs_a"):
        if row.get(name) is not None and row[name] < 0:
            raise ValueError("Negative absolute/RMS summary")
    if row.get("error_sample_count") is not None and not 0 <= row["error_sample_count"] <= row["sample_count"]:
        raise ValueError("Invalid error sample count")
    row["row_sha256"] = digest(row)
    return row


def prepare(directory, container):
    load = lambda name: json.loads((directory / name).read_text())
    old = load("old-executions.json")
    pg = load("old-joint-summaries.json")
    influx = load("influx-joint-summaries.json")
    events = load("influx-events.json")
    context = json.loads(psql(container, CONTEXT_SQL, readonly=True))
    current = {row["source_execution_key"]: row for row in context}
    metadata = {row["test_id"]: row for row in old}
    if len(current) != len(context) or len(metadata) != len(old):
        raise ValueError("Ambiguous test identifiers")
    keys = sorted(set(metadata) | {x["test_id"] for x in influx + events})
    executions = []
    for key in keys:
        prior = current.get(key)
        row = {"source_test_id": key, "existing_execution_id": prior["id"] if prior else None,
               "mapping_status": "EXACT_EXISTING" if prior else "MISSING_EXECUTION" if key in metadata else "UNMAPPED",
               "source_snapshot": metadata.get(key, {"test_id": key, "identity_confirmed": False})}
        if prior:
            if key not in metadata:
                raise ValueError("Cannot resolve an existing execution without old metadata")
            original = metadata[key]
            if original["bench_id"] != prior["station_code"] or original["test_case_id"] != prior["case_code"]:
                raise ValueError("Station/test case identity mismatch")
            if utc(original["started_at"]) != utc(prior["normalized_started_at"]):
                raise ValueError("Execution start identity mismatch")
        row["row_sha256"] = digest(row)
        executions.append(row)
    summaries = []
    for raw in pg:
        if raw["test_id"] not in metadata:
            raise ValueError("PostgreSQL summary has no source execution")
        row = {"source_test_id": raw["test_id"], "source_kind": "POSTGRES_SUMMARY",
               "source_joint": raw["joint"], "proposed_subject_code": proposed_subject(raw["test_id"], raw["joint"]),
               "sample_count": raw["sample_count"], "error_sample_count": raw["error_count"],
               "provenance": {"database": "robot_test_meta", "table": "public.execution_joint_metrics",
                              "created_at": raw["created_at"], "updated_at": raw["updated_at"],
                              "aggregation_version": "source-persisted-unknown", "window_verified": False,
                              "original_row_sha256": digest(raw), "tracking_unit": "rad",
                              "torque_semantics": "absolute", "sample_rate_verified": False}}
        row.update({name: raw.get(name) for name in NUMERIC})
        summaries.append(checked_summary(row))
    for raw in influx:
        row = {"source_test_id": raw["test_id"], "source_kind": "INFLUX_AGGREGATE",
               "source_joint": raw["joint"], "proposed_subject_code": proposed_subject(raw["test_id"], raw["joint"]),
               "source_robot_id": raw.get("robot_id"), "source_limb": raw.get("limb"),
               "first_observed_at": utc(raw["first_at"], True), "last_observed_at": utc(raw["last_at"], True),
               "sample_count": raw["sample_count"], "error_sample_count": raw["error_sample_count"],
               "provenance": {"database": "robot_test", "table": "joint_metrics",
                              "aggregation_version": "local-scalar-v1", "aggregation_sql_sha256": hashlib.sha256(INFLUX_AGGREGATE_SQL.encode()).hexdigest(),
                              "first_time_ns_utc": raw["first_at"], "last_time_ns_utc": raw["last_at"],
                              "sample_rate_verified": False, "tracking_unit": "rad", "torque_semantics": "absolute",
                              "valid_counts": {k: raw[k] for k in ("temp_count", "torque_count", "tracking_count", "voltage_count", "current_count")}}}
        row.update({name: raw.get(name) for name in NUMERIC})
        row["temp_rise_c"] = (raw["temp_end_c"] - raw["temp_start_c"]
                              if raw.get("temp_end_c") is not None and raw.get("temp_start_c") is not None else None)
        original = metadata.get(raw["test_id"])
        if original:
            # An exact ID alone does not prove device or aggregation window validity.
            row["provenance"]["robot_matches_metadata"] = raw.get("robot_id") in {original["sample_id"], original["robot_id"]}
            start = utc(original["started_at"])
            end = utc(original["ended_at"])
            row["provenance"]["within_execution_window"] = bool(end and
                datetime.fromisoformat(start) <= datetime.fromisoformat(row["first_observed_at"])
                <= datetime.fromisoformat(row["last_observed_at"]) <= datetime.fromisoformat(end))
        summaries.append(checked_summary(row))
    identities = [(x["source_test_id"], x["source_kind"], x["source_joint"]) for x in summaries]
    if len(set(identities)) != len(identities):
        raise ValueError("Mixed robot/limb identities in one test joint; manual review required")
    recovered_events = []
    for raw in events:
        row = {"source_test_id": raw["test_id"], "observed_at": utc(raw["time"], True),
               "event_type": raw["event_type"], "source_joint": raw.get("joint"),
               "source_robot_id": raw.get("robot_id"), "source_limb": raw.get("limb")}
        row.update({name: raw.get(name) for name in ("error_id", "duration_s", "elapsed_s", "fault_count")})
        row["row_sha256"] = digest(row)
        recovered_events.append(row)
    if len({x["row_sha256"] for x in recovered_events}) != len(recovered_events):
        raise ValueError("Duplicate source events")
    data = {"context_sha256": digest(context), "context": context, "executions": executions,
            "summaries": summaries, "events": recovered_events,
            "counts": {"executions": len(executions), "summaries": len(summaries),
                       "events": len(recovered_events), "mapping": dict(Counter(x["mapping_status"] for x in executions)),
                       "summary_sources": dict(Counter(x["source_kind"] for x in summaries))}}
    data["dataset_sha256"] = digest(data)
    data["batch_key"] = "MOBILE-DISK-RECOVERY-20261012-" + data["dataset_sha256"][:16]
    private_json(directory / "prepared.json", data)
    return {"batch_key": data["batch_key"], "dataset_sha256": data["dataset_sha256"], "counts": data["counts"]}


def insert_rows(table, batch_key, rows):
    if not rows:
        return ""
    fields = sorted(set().union(*(row.keys() for row in rows)))
    if not all(re.fullmatch(r"[a-z_][a-z0-9_]*", x) for x in fields):
        raise ValueError("Invalid column")
    payload = [{"batch_key": batch_key, **row} for row in rows]
    fields = ["batch_key"] + fields
    names = ",".join(fields)
    return f"INSERT INTO integration.{table}({names}) SELECT {names} FROM jsonb_populate_recordset(NULL::integration.{table},{literal(encoded(payload))}::jsonb);\n"


def apply(directory, container, expected_sha, actor):
    data = json.loads((directory / "prepared.json").read_text())
    evidence = dict(data)
    evidence.pop("batch_key")
    evidence.pop("dataset_sha256")
    if data["dataset_sha256"] != expected_sha or digest(evidence) != expected_sha:
        raise ValueError("Prepared data checksum mismatch")
    if digest(json.loads(psql(container, CONTEXT_SQL, readonly=True))) != data["context_sha256"]:
        raise ValueError("Target execution context changed since validation")
    schema = SCHEMA.read_text().replace("__RP1_APP_USER__", "rp1_app").replace("__RP1_READONLY_USER__", "rp1_readonly")
    batch = data["batch_key"]
    sql = "BEGIN; SET LOCAL standard_conforming_strings=on; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='60s';\n"
    sql += "SELECT pg_advisory_xact_lock(hashtextextended('rp1-legacy-recovery',0));\n"
    sql += "LOCK TABLE " + ",".join(FORMAL_TABLES) + " IN SHARE MODE;\n"
    sql += f"DO $$ BEGIN IF ({CONTEXT_SQL})::jsonb IS DISTINCT FROM {literal(encoded(data['context']))}::jsonb THEN RAISE EXCEPTION 'Target execution context changed before transaction lock'; END IF; END $$;\n"
    sql += f"SELECT iam.set_request_context((SELECT public_id FROM iam.app_user WHERE username={literal(actor)} AND enabled),{literal(batch)},'恢复移动硬盘历史摘要，保持未审核，不进入正式统计');\n"
    sql += "DO $$ BEGIN IF NOT iam.is_system_admin() THEN RAISE EXCEPTION 'Enabled human system administrator required'; END IF; END $$;\n"
    sql += "CREATE TEMP TABLE recovery_formal_before(table_name text PRIMARY KEY,fingerprint text);\n"
    for table in FORMAL_TABLES:
        sql += f"INSERT INTO recovery_formal_before SELECT {literal(table)},md5(coalesce(string_agg(to_jsonb(t)::text,'' ORDER BY to_jsonb(t)::text),'')) FROM {table} t;\n"
    sql += schema + "\n"
    # One transaction, an advisory lock, no upsert of source evidence, and a hash guard.
    sql += f"""DO $guard$ BEGIN
 IF EXISTS(SELECT 1 FROM integration.legacy_recovery_batch WHERE batch_key={literal(batch)}
           AND dataset_sha256 <> {literal(expected_sha)}) THEN RAISE EXCEPTION 'Batch content conflict'; END IF;
 END $guard$;
"""
    body = f"INSERT INTO integration.legacy_recovery_batch(batch_key,dataset_sha256,schema_sha256,actor_user_id,source_note,counts) VALUES ({literal(batch)},{literal(expected_sha)},{literal(hashlib.sha256(SCHEMA.read_bytes()).hexdigest())},iam.current_user_id(),'移动硬盘 robot_test_meta 与本地 InfluxDB 标量汇总；不包含原始 CSV 或完整时序',{literal(encoded(data['counts']))}::jsonb);\n"
    body += insert_rows("legacy_recovered_execution", batch, data["executions"])
    body += insert_rows("legacy_joint_summary", batch, data["summaries"])
    body += insert_rows("legacy_recovered_event", batch, data["events"])
    # Execute data writes only on the first import; exact retries are no-ops.
    tag = "$recovery_" + hashlib.sha256(body.encode()).hexdigest() + "$"
    while tag in body:
        tag = tag[:-1] + "x$"
    sql += f"DO {tag} BEGIN IF NOT EXISTS(SELECT 1 FROM integration.legacy_recovery_batch WHERE batch_key={literal(batch)}) THEN\n{body}END IF; END {tag};\n"
    for table in FORMAL_TABLES:
        sql += f"DO $$ BEGIN IF (SELECT fingerprint FROM recovery_formal_before WHERE table_name={literal(table)}) IS DISTINCT FROM (SELECT md5(coalesce(string_agg(to_jsonb(t)::text,'' ORDER BY to_jsonb(t)::text),'')) FROM {table} t) THEN RAISE EXCEPTION 'Formal table changed: {table}'; END IF; END $$;\n"
    for table, key in (("legacy_recovered_execution", "executions"), ("legacy_joint_summary", "summaries"), ("legacy_recovered_event", "events")):
        sql += f"DO $$ BEGIN IF (SELECT count(*) FROM integration.{table} WHERE batch_key={literal(batch)}) <> {len(data[key])} THEN RAISE EXCEPTION 'Imported count mismatch'; END IF; END $$;\n"
    sql += f"SELECT json_build_object('batch_key',{literal(batch)},'counts',{literal(encoded(data['counts']))}::jsonb,'formal_tables_unchanged',true,'review_state','UNREVIEWED'); COMMIT;\n"
    output = psql(container, sql)
    return json.loads(output.splitlines()[-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "apply"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--sha256")
    parser.add_argument("--actor", default="admin")
    args = parser.parse_args()
    if args.mode == "apply" and not re.fullmatch(r"[0-9a-f]{64}", args.sha256 or ""):
        parser.error("apply requires independently checked --sha256")
    result = (prepare(args.directory, args.container) if args.mode == "prepare"
              else apply(args.directory, args.container, args.sha256, args.actor))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
