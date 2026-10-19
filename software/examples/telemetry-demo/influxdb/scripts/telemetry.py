"""Generate synthetic points and exercise InfluxDB v2 health/write/query."""

import argparse
import json
import math
import sys
import time
from pathlib import Path

from client import FIELDS, InfluxClient, InfluxError, line_protocol, load_env


def generate(run_id: str, case_id: str, subject: str, start_ns: int) -> list[str]:
    tags = {"run_id": run_id, "case_id": case_id, "subject": subject, "source_kind": "synthetic"}
    points = []
    for index in range(600):
        seconds = index / 5
        if 55 <= seconds < 60:
            continue  # A deliberate 5-second gap, not a held or interpolated value.
        command = 20 * math.sin(seconds * math.pi / 10)
        feedback = 20 * math.sin((seconds - 0.08) * math.pi / 10)
        fields = {"cmd_pos_deg": command, "pos_deg": feedback,
                  "tracking_error_deg": feedback - command,
                  "temp_c": 25 + seconds * 0.04,
                  "spd_deg_s": 2 * math.pi * math.cos((seconds - 0.08) * math.pi / 10),
                  "torque_nm": None}
        points.append(line_protocol(tags, fields, start_ns + index * 200_000_000))
    return points


def query_text(client: InfluxClient, run_id: str, start: str) -> str:
    # JSON string encoding is also valid Flux quoted-string escaping.
    return f'''from(bucket: {json.dumps(client.bucket)})
  |> range(start: {start})
  |> filter(fn: (r) => r._measurement == "joint_telemetry" and r.run_id == {json.dumps(run_id)})
  |> keep(columns: ["_time", "_field", "_value", "run_id", "case_id", "subject", "source_kind"])
  |> sort(columns: ["_time"])
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("health")
    demo = sub.add_parser("generate")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--run-id", default="demo-synthetic")
    demo.add_argument("--case-id", default="SYS-REL-001")
    demo.add_argument("--subject", default="demo-joint-01")
    write = sub.add_parser("write")
    write.add_argument("file", type=Path)
    write.add_argument("--batch-size", type=int, default=1000)
    query = sub.add_parser("query")
    query.add_argument("--run-id", required=True)
    query.add_argument("--start-minutes", type=int, default=15)
    query.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        load_env(args.env_file)
        client = InfluxClient()
        if args.command == "generate":
            points = generate(args.run_id, args.case_id, args.subject, time.time_ns() - 120_000_000_000)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text("\n".join(points) + "\n", encoding="utf-8")
            print(f"Synthetic points: {len(points)}; run_id={args.run_id}; source_kind=synthetic; torque_nm absent")
        elif args.command == "health":
            health = client.health()
            print(json.dumps({"status": health.get("status"), "version": health.get("version")}))
            return 0 if health.get("status") == "pass" else 1
        elif args.command == "write":
            if args.batch_size < 1 or args.batch_size > 10000:
                raise ValueError("batch-size must be between 1 and 10000")
            count = 0
            batch = []
            with args.file.open(encoding="utf-8") as stream:
                for line in stream:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    batch.append(line)
                    if len(batch) == args.batch_size:
                        client.write("\n".join(batch))
                        count += len(batch)
                        batch.clear()
            if batch:
                client.write("\n".join(batch))
                count += len(batch)
            print(f"Wrote {count} points with nanosecond precision.")
        elif args.command == "query":
            if args.start_minutes < 1:
                raise ValueError("start-minutes must be positive")
            output = client.query(query_text(client, args.run_id, f"-{args.start_minutes}m"))
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(output, encoding="utf-8")
                print(f"Query saved to {args.output}")
            else:
                print(output, end="")
    except (ValueError, InfluxError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
