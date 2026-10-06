#!/usr/bin/env python3
"""Ingest an exported Factory HMI bundle independently of desktop approvals."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import stat
import sys
import tarfile
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import uuid4

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml

from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.reliability_platform import (
    ReliabilityPlatformClient, ReliabilityUploaderWorker, bridge_manifest,
    enqueue_bundle, inspect_bundle,
)


RIGHT_ARM_SUBJECTS = {
    "right_shoulder_pitch_joint": "SYS-SHOULDER-PITCH-R",
    "right_shoulder_roll_joint": "SYS-SHOULDER-ROLL-R",
    "right_shoulder_yaw_joint": "SYS-SHOULDER-YAW-R",
    "right_elbow_joint": "SYS-ELBOW-PITCH-R",
    "right_wrist_roll_joint": "SYS-WRIST-ROLL-R",
    "right_wrist_yaw_joint": "SYS-WRIST-YAW-R",
    "right_wrist_pitch_joint": "SYS-WRIST-PITCH-R",
}


def read_mapping(path: Path, *, private: bool = False) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("configuration must be a regular file")
    if private and stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise PermissionError("credential context must have mode 0600 or stricter")
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("configuration must contain a JSON/YAML object")
    return value


def read_credentials(context: Mapping[str, Any], env_file: Path | None) -> str:
    """Accept a private context or a private key env file; never CLI key text."""
    token = str(context.get("api_key") or "").strip()
    if env_file is not None:
        if env_file.is_symlink() or not env_file.is_file() or stat.S_IMODE(env_file.stat().st_mode) & 0o077:
            raise PermissionError("credential env file must be a regular private file (0600)")
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            key, separator, value = stripped.partition("=")
            if separator and key.strip() == "RP1_RELIABILITY_SERVICE_KEY":
                token = value.strip().strip("\"'")
    if not token:
        raise ValueError("api_key in private context or RP1_RELIABILITY_SERVICE_KEY in private env file is required")
    return token


def generate_demo(output: Path, duration: float = 12.0, rate: float = 20.0) -> Path:
    """Use the real ReportManager with generated CSV; no CAN or robot involved."""
    from factory_hmi.core.report import ReportManager

    if not 1 <= duration <= 60 or not 1 <= rate <= 100:
        raise ValueError("demo duration must be 1..60 s and sample rate 1..100 Hz")
    output.mkdir(parents=True, exist_ok=True)
    identifier = uuid4()
    started = datetime.now(timezone.utc)
    test_id = f"synthetic-factory-{identifier.hex}"
    csv_path = output / f"{test_id}.csv"
    fields = ["time_s"] + [
        f"{joint}_{suffix}" for joint in RIGHT_ARM_SUBJECTS
        for suffix in ("cmd_pos_rad", "pos_rad", "spd_rad_s", "torque_nm", "temp_c", "error_id")
    ]
    samples = max(1, math.floor(duration * rate))
    with csv_path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for index in range(samples + 1):
            elapsed = duration * index / samples
            row: dict[str, float] = {"time_s": elapsed}
            for offset, joint in enumerate(RIGHT_ARM_SUBJECTS):
                angle = 0.1 * math.sin(2 * math.pi * elapsed / duration + offset * 0.2)
                row.update({
                    f"{joint}_cmd_pos_rad": angle,
                    f"{joint}_pos_rad": angle - 0.002,
                    f"{joint}_spd_rad_s": 0.1 * 2 * math.pi / duration * math.cos(2 * math.pi * elapsed / duration + offset * 0.2),
                    f"{joint}_torque_nm": 1.0 + offset * 0.1,
                    f"{joint}_temp_c": 25.0 + elapsed * 0.01,
                    f"{joint}_error_id": 0,
                })
            writer.writerow(row)
    context = {
        "execution_uuid": str(identifier), "test_id": test_id,
        "test_case_id": "synthetic-factory-aging", "test_case_version": "software-demo",
        "sample_id": "SYNTHETIC-NO-ROBOT", "bench_id": "SYNTHETIC-NO-BENCH",
        "started_at": started.isoformat(), "ended_at": (started + timedelta(seconds=duration)).isoformat(),
        "active_seconds": duration, "record_rate_hz": samples / duration,
        "completed_cycles": 1, "outcome": "interrupted", "module_name": "right_arm",
        "stop_reason": "synthetic data fixture; no physical test was performed",
        "entries": [{"joint_name": joint, "motor_id": index + 1, "module_name": "right_arm"} for index, joint in enumerate(RIGHT_ARM_SUBJECTS)],
        "software": {"hmi_version": "synthetic-factory-report-demo"},
        # This is not a completed physical execution. No thresholds are invented.
        "config": {"source_kind": "synthetic", "backend": "fake"},
    }
    result = ReportManager(output / "reports").finalize(csv_path, context)
    (output / "subject-map.yaml").write_text(
        yaml.safe_dump(RIGHT_ARM_SUBJECTS, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )
    print(json.dumps({
        "source_kind": "synthetic", "bundle": str(result.bundle_path),
        "subject_map": str(output / "subject-map.yaml"),
        "note": "generated Factory ReportManager fixture; no hardware test",
    }, ensure_ascii=False))
    return result.bundle_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("generate-demo", help="generate a synthetic Factory ReportManager bundle")
    demo.add_argument("--output", type=Path, required=True)
    demo.add_argument("--duration", type=float, default=12)
    demo.add_argument("--sample-rate", type=float, default=20)
    for name in ("upload", "flush"):
        command = commands.add_parser(name, help="ingest one bundle" if name == "upload" else "retry queued bundles")
        command.add_argument("--context", type=Path, required=True, help="private JSON/YAML containing database UUIDs and optionally api_key")
        command.add_argument("--key-env-file", type=Path, help="private file containing RP1_RELIABILITY_SERVICE_KEY=...")
        command.add_argument("--base-url", required=True)
        command.add_argument("--outbox", type=Path, required=True, help="dedicated bridge SQLite path, separate from GUI sync/outbox.sqlite3")
        if name == "upload":
            command.add_argument("--bundle", type=Path, required=True)
            command.add_argument("--subject-map", type=Path, required=True, help="JSON/YAML joint name -> catalog telemetry subject code")
            command.add_argument("--source-kind", choices=("synthetic", "real"), default="synthetic")
            command.add_argument("--evidence", type=Path, help="physical-recording evidence JSON/YAML, required for real ingestion")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "generate-demo":
            generate_demo(args.output, args.duration, args.sample_rate)
            return 0
        context = read_mapping(args.context, private=True)
        key = read_credentials(context, args.key_env_file)
        client = ReliabilityPlatformClient(args.base_url, key)
        if args.outbox.name == "outbox.sqlite3" and args.outbox.parent.name == "sync":
            raise ValueError("use a dedicated bridge outbox, not the desktop sync/outbox.sqlite3")
        outbox = Outbox(args.outbox)
        target = None
        if args.command == "upload":
            bundle = inspect_bundle(args.bundle)
            mapping = read_mapping(args.subject_map)
            if "subject_map" in mapping:
                mapping = mapping["subject_map"]
            if not isinstance(mapping, Mapping):
                raise ValueError("subject map must be an object")
            evidence = read_mapping(args.evidence) if args.evidence else None
            manifest = bridge_manifest(bundle, context, mapping, source_kind=args.source_kind, evidence=evidence)
            target = enqueue_bundle(outbox, bundle, manifest).execution_uuid
        accepted = ReliabilityUploaderWorker(outbox, client).process_once()
        items = [outbox.get(target)] if target else outbox.list(limit=100)
        result = [{
            "execution_uuid": item.execution_uuid, "ingestion_state": item.remote_state or item.state,
            "source_kind": item.manifest.get("source_kind"), "last_error": item.last_error,
            "execution_id": item.submission_id,
            "submitted_verdict": item.manifest.get("status"),
        } for item in items if item is not None]
        print(json.dumps({"accepted": accepted, "items": result, "note": "ingestion acceptance is not desktop approval; binary bundle remains local"}, ensure_ascii=False))
        return 0 if all(item is not None and item.remote_state == "INGESTED" for item in items) else 1
    except (OSError, ValueError, KeyError, tarfile.TarError) as exc:
        print(f"Factory bundle bridge: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
