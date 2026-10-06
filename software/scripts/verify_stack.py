"""Read-only smoke check for the local stack and its latest synthetic run.

Checks the provisioned dashboard's actual queries through Grafana. Credentials
stay in the local .env file and are never included in the report.
"""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import math
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SOFTWARE = Path(__file__).resolve().parents[1]
DASHBOARD_UID = "rp1-joint-telemetry"
DATASOURCE_UID = "rp1-influxdb"


def read_env(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, separator, value = line.partition("=")
            if not separator:
                raise ValueError("Invalid local environment file")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def request(
    url: str, *, headers: dict | None = None, payload: dict | None = None
) -> bytes:
    body = json.dumps(payload).encode() if payload is not None else None
    merged = {"Content-Type": "application/json", **(headers or {})}
    call = urllib.request.Request(url, data=body, headers=merged)
    try:
        with urllib.request.urlopen(call, timeout=20) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"HTTP {exc.code} from {urllib.parse.urlsplit(url).path}"
        ) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("A stack endpoint is unavailable") from None


def latest_run(runtime: Path) -> tuple[dict, str]:
    files = sorted(
        (runtime / "runs").glob("*/metadata.json"), key=lambda p: p.stat().st_mtime
    )
    if not files:
        raise ValueError("Run scripts/run-demo.sh before checking the demo")
    metadata = json.loads(files[-1].read_text(encoding="utf-8"))
    if (
        metadata.get("source_kind") != "synthetic"
        or metadata.get("test_outcome") != "INCONCLUSIVE"
    ):
        raise ValueError(
            "Latest run must be an explicitly synthetic, inconclusive demo"
        )
    with sqlite3.connect(f"file:{runtime / 'outbox.sqlite3'}?mode=ro", uri=True) as db:
        pending = db.execute(
            "SELECT count(*) FROM outbox WHERE status='pending'"
        ).fetchone()[0]
        row = db.execute(
            "SELECT execution_id FROM execution_resolution WHERE producer_execution_key=?",
            (files[-1].parent.name,),
        ).fetchone()
    if pending or not row:
        raise ValueError(
            "The local outbox has unfinished requests or no execution mapping"
        )
    return metadata, row[0]


def verify_influx(url: str, env: dict, execution_id: str) -> int:
    flux = (
        f"from(bucket: {json.dumps(env['INFLUX_BUCKET'])}) |> range(start: -24h) "
        '|> filter(fn: (r) => r._measurement == "joint_telemetry" '
        f"and r.run_id == {json.dumps(execution_id)} "
        'and r.source_kind == "synthetic") |> limit(n: 10)'
    )
    query_url = (
        url + "/api/v2/query?" + urllib.parse.urlencode({"org": env["INFLUX_ORG"]})
    )
    raw = request(
        query_url,
        headers={"Authorization": "Token " + env["INFLUX_TOKEN"], "Accept": "text/csv"},
        payload={"query": flux, "type": "flux"},
    ).decode()
    rows = [
        row
        for row in csv.reader(io.StringIO(raw))
        if execution_id in row and "synthetic" in row
    ]
    if not rows:
        raise ValueError(
            "The Influx export is not visible yet; retry after the worker poll interval"
        )
    return len(rows)


def verify_grafana(
    url: str, env: dict, execution_id: str | None, metadata: dict | None = None
) -> dict:
    credential = f"{env['GRAFANA_ADMIN_USER']}:{env['GRAFANA_ADMIN_PASSWORD']}".encode()
    headers = {"Authorization": "Basic " + base64.b64encode(credential).decode()}
    dashboard = json.loads(
        request(url + f"/api/dashboards/uid/{DASHBOARD_UID}", headers=headers)
    )["dashboard"]
    source = json.loads(
        request(url + f"/api/datasources/uid/{DATASOURCE_UID}", headers=headers)
    )
    if source["jsonData"]["version"] != "Flux":
        raise ValueError("Grafana datasource must use Flux")
    result = {"dashboard": dashboard["uid"], "panels": len(dashboard["panels"])}
    if execution_id is None:
        return result
    queries = []
    numeric_refs = set()
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            model = dict(target)
            model["refId"] = str(panel["id"]) + target["refId"]
            model["datasource"] = {"type": "influxdb", "uid": DATASOURCE_UID}
            model["query"] = (
                model["query"]
                .replace("${run_id:json}", json.dumps(execution_id))
                .replace("${subject:regex}", ".*")
            )
            model.update(maxDataPoints=1000, intervalMs=1000)
            queries.append(model)
            if panel["type"] == "timeseries":
                numeric_refs.add(model["refId"])
    for variable in dashboard["templating"]["list"]:
        if variable["type"] == "query":
            queries.append(
                {
                    "refId": "variable_" + variable["name"],
                    "datasource": {"type": "influxdb", "uid": DATASOURCE_UID},
                    "query": variable["query"].replace(
                        "${run_id:json}", json.dumps(execution_id)
                    ),
                    "maxDataPoints": 1000,
                    "intervalMs": 1000,
                }
            )
    if metadata is None:
        raise ValueError("Demo metadata is needed to select its time range")
    started_ms = (
        int(datetime.fromisoformat(metadata["started_at"].replace("Z", "+00:00")).timestamp() * 1000) - 1000
    )
    ended_ms = (
        int(datetime.fromisoformat(metadata["ended_at"].replace("Z", "+00:00")).timestamp() * 1000) + 1000
    )
    response = json.loads(
        request(
            url + "/api/ds/query",
            headers=headers,
            payload={"from": str(started_ms), "to": str(ended_ms), "queries": queries},
        )
    )
    points = {}
    for query in queries:
        ref = query["refId"]
        item = response["results"].get(ref, {})
        if item.get("error"):
            raise ValueError(f"Grafana query {ref} failed")
        count = 0
        for frame in item.get("frames", []):
            for field, values in zip(
                frame["schema"]["fields"], frame["data"]["values"], strict=True
            ):
                if ref in numeric_refs and field["type"] == "number":
                    count += sum(
                        isinstance(v, (int, float)) and math.isfinite(v) for v in values
                    )
                elif ref not in numeric_refs and field["type"] == "string":
                    count += sum(isinstance(v, str) and bool(v) for v in values)
        if not count:
            raise ValueError(f"Grafana query {ref} has no usable data")
        points[ref] = count
    result["query_values"] = points
    return result


def inspect_stack(env_file: Path, *, demo: bool) -> dict:
    env = read_env(env_file)
    api_url = "http://127.0.0.1:" + env.get("API_PORT", "18080")
    influx_url = "http://127.0.0.1:" + env.get("INFLUX_PORT", "18086")
    grafana_url = "http://127.0.0.1:" + env.get("GRAFANA_PORT", "13000")
    report = {
        "api": json.loads(request(api_url + "/readyz")),
        "influx": json.loads(request(influx_url + "/health"))["status"],
        "grafana": json.loads(request(grafana_url + "/api/health"))["database"],
    }
    execution_id = None
    metadata = None
    if demo:
        metadata, execution_id = latest_run(env_file.parent / ".runtime")
        report["demo"] = {
            "source_kind": metadata["source_kind"],
            "outcome": metadata["test_outcome"],
            "elapsed_seconds": metadata["elapsed_seconds"],
            "active_seconds": metadata["active_seconds"],
            "influx_rows_checked": verify_influx(influx_url, env, execution_id),
        }
    report["dashboard"] = verify_grafana(grafana_url, env, execution_id, metadata)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=SOFTWARE / ".env")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="also check latest upload, Influx export and dashboard queries",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=30,
        help="allow asynchronous export to finish (0–60)",
    )
    args = parser.parse_args()
    if not math.isfinite(args.wait_seconds) or not 0 <= args.wait_seconds <= 60:
        parser.error("--wait-seconds must be in [0, 60]")
    deadline = time.monotonic() + args.wait_seconds
    while True:
        try:
            report = inspect_stack(args.env_file, demo=args.demo)
        except (RuntimeError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)
        else:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, sqlite3.Error) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
