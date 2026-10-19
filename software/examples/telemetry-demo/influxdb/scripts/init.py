"""Initialize a fresh InfluxDB OSS v2 instance through /api/v2/setup."""

import argparse
import json
import os
import sys
import time
import urllib.parse

from client import InfluxClient, InfluxError, load_env


def initialize(wait_seconds: float) -> None:
    client = InfluxClient()
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            if client.health().get("status") == "pass":
                break
        except InfluxError:
            pass
        if time.monotonic() >= deadline:
            raise InfluxError("InfluxDB was not ready before the setup timeout")
        time.sleep(1)
    allowed = json.loads(client.request("GET", "/api/v2/setup", auth=False))["allowed"]
    if allowed:
        username = os.environ.get("INFLUX_ADMIN_USERNAME", "")
        password = os.environ.get("INFLUX_ADMIN_PASSWORD", "")
        if not username or len(password) < 8 or not client.token:
            raise ValueError("Set INFLUX_ADMIN_USERNAME, INFLUX_ADMIN_PASSWORD (at least 8 characters), and INFLUX_TOKEN")
        retention = int(os.getenv("INFLUX_RETENTION_SECONDS", "2592000"))
        if retention <= 0:
            raise ValueError("INFLUX_RETENTION_SECONDS must be positive")
        payload = {"username": username, "password": password, "org": client.org,
                   "bucket": client.bucket, "token": client.token, "retentionPeriodSeconds": retention}
        client.request("POST", "/api/v2/setup", json.dumps(payload).encode(), auth=False)
        print("Initialized InfluxDB v2; credentials are not printed.")
    # Also validate a reused volume. Changed .env credentials are not a migration.
    params = urllib.parse.urlencode({"name": client.bucket, "org": client.org})
    buckets = json.loads(client.request("GET", f"/api/v2/buckets?{params}"))["buckets"]
    if not any(item["name"] == client.bucket for item in buckets):
        raise InfluxError("Configured bucket was not found; check organization and credentials")
    print(f"Bucket ready: {client.org}/{client.bucket}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="Local KEY=value file; never committed")
    parser.add_argument("--wait-seconds", type=float, default=30)
    args = parser.parse_args()
    try:
        load_env(args.env_file)
        initialize(args.wait_seconds)
    except (ValueError, InfluxError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
