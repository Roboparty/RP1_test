#!/usr/bin/env python3
"""Back up current PostgreSQL plus configuration during a maintenance window."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tarfile

from archive_backup import write_archive_snapshot

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "python:3.12-slim@sha256:ddb0207ae1f0356c2b724d740769b0c5f5f51cc54a0525178f721825f78fe74c"


def compose(*args, **kwargs):
    return subprocess.run([str(ROOT / "scripts/compose.sh"), *args], cwd=ROOT, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--maintenance", action="store_true", help="Stop platform writers temporarily; HMI remains local")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.maintenance:
        parser.error("Schedule a maintenance window and add --maintenance")
    settings = dict(line.split("=", 1) for line in (ROOT / ".env").read_text().splitlines() if line and not line.startswith("#"))
    project = settings["COMPOSE_PROJECT_NAME"]
    os.umask(0o077)
    output = args.output or ROOT / "backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=False)
    originally_running = compose("ps", "--services", "--status", "running", capture_output=True, text=True).stdout.split()
    writers = [service for service in ("web", "api", "worker", "archive-worker") if service in originally_running]
    try:
        if writers:
            compose("stop", "-t", "60", *writers)
        with (output / "postgres.dump").open("wb") as handle:
            compose("exec", "-T", "postgres", "pg_dump", "-U", settings["POSTGRES_USER"], "-d", settings["POSTGRES_DB"], "-Fc", stdout=handle)
        with (output / "postgres.dump").open("rb") as handle:
            compose("exec", "-T", "postgres", "pg_restore", "--list", stdin=handle, stdout=subprocess.DEVNULL)
        api_container = compose("ps", "--all", "--quiet", "api", capture_output=True, text=True).stdout.strip()
        if not api_container:
            raise RuntimeError("API container archive mount could not be located")
        write_archive_snapshot(output, api_container, IMAGE)
        with tarfile.open(output / "config.tar.gz", "w:gz") as archive:
            for relative in (".env", "deployment/secrets"):
                archive.add(ROOT / relative, arcname=relative)
        metadata = {"version": (ROOT / "VERSION").read_text().strip(), "created_at": datetime.now(timezone.utc).isoformat(), "project": project, "archive_format": 1}
        (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
        checksums = {p.name: hashlib.file_digest(p.open("rb"), "sha256").hexdigest() for p in output.iterdir() if p.is_file()}
        (output / "SHA256SUMS").write_text("".join(f"{value}  {name}\n" for name, value in sorted(checksums.items())))
        print("Backup completed: " + str(output))
        print("Contains credentials and private keys. Store on restricted, encrypted off-machine storage.")
    finally:
        if writers:
            compose("up", "-d", "--no-deps", "--wait", "--wait-timeout", "300", *writers)


if __name__ == "__main__":
    main()
