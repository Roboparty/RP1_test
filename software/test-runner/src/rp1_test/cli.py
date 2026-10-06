"""Command-line parser and dispatch for rp1-test."""

from __future__ import annotations

import argparse
from pathlib import Path

from .commands.common import load_config as load_config
from .commands.hardware import hardware_preflight, hardware_replay
from .commands.platform import platform_enqueue_run, platform_flush
from .commands.simulation import simulation_replay
from .commands.trajectory import trajectory_generate, trajectory_record, trajectory_validate
from .generators import GENERATORS


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rp1-test")
    sub = parser.add_subparsers(dest="group", required=True)
    trajectory = sub.add_parser("trajectory")
    trajectory_sub = trajectory.add_subparsers(dest="command", required=True)

    generate_parser = trajectory_sub.add_parser("generate")
    generate_parser.add_argument("--config", type=Path, required=True)
    generate_parser.add_argument("--profile", choices=sorted(GENERATORS), required=True)
    generate_parser.add_argument("--params", help="JSON object or @file.json")
    generate_parser.add_argument("--fps", type=float, default=50.0)
    generate_parser.add_argument("--model", type=Path)
    generate_parser.add_argument("--output", type=Path, required=True)
    generate_parser.set_defaults(function=trajectory_generate)

    record_parser = trajectory_sub.add_parser("record")
    record_parser.add_argument("--config", type=Path, required=True)
    record_parser.add_argument("--model", type=Path)
    record_parser.add_argument("--mode", choices=("joint", "ik"), required=True)
    record_parser.add_argument("--keyframes", type=Path, required=True)
    record_parser.add_argument("--site", default="ik_end_site")
    record_parser.add_argument("--fps", type=float, default=50.0)
    record_parser.add_argument("--segment-seconds", type=float, default=1.0)
    record_parser.add_argument("--output", type=Path, required=True)
    record_parser.add_argument("--dry-run", action="store_true")
    record_parser.set_defaults(function=trajectory_record)

    validate_parser = trajectory_sub.add_parser("validate")
    validate_parser.add_argument("trajectory", type=Path)
    validate_parser.add_argument("--config", type=Path, required=True)
    validate_parser.add_argument("--model", type=Path)
    validate_parser.set_defaults(function=trajectory_validate)

    simulation = sub.add_parser("sim")
    simulation_sub = simulation.add_subparsers(dest="command", required=True)
    replay_parser = simulation_sub.add_parser("replay")
    replay_parser.add_argument("trajectory", type=Path)
    replay_parser.add_argument("--config", type=Path, required=True)
    replay_parser.add_argument("--model", type=Path)
    replay_parser.add_argument("--loop", action="store_true")
    replay_parser.add_argument("--cycles", type=int, default=0)
    replay_parser.add_argument("--headless", action="store_true")
    replay_parser.add_argument("--dry-run", action="store_true")
    replay_parser.set_defaults(function=simulation_replay)

    hardware = sub.add_parser("hardware")
    hardware_sub = hardware.add_subparsers(dest="command", required=True)
    for name, function in (("preflight", hardware_preflight), ("replay", hardware_replay)):
        item = hardware_sub.add_parser(name)
        item.add_argument("trajectory", type=Path)
        item.add_argument("--config", type=Path, required=True)
        item.add_argument("--model", type=Path)
        item.set_defaults(function=function)
        if name == "replay":
            item.add_argument("--dry-run", action="store_true")
            item.add_argument("--real-hardware", action="store_true")
            item.add_argument("--acknowledge", default="")
            item.add_argument("--loop", action="store_true")
            item.add_argument("--run-id")
            item.add_argument("--output-dir", type=Path, default=Path("runs"))

    platform = sub.add_parser("platform")
    platform_sub = platform.add_subparsers(dest="command", required=True)
    enqueue = platform_sub.add_parser("enqueue-run")
    enqueue.add_argument("run_dir", type=Path)
    enqueue.add_argument("--outbox", type=Path, required=True)
    enqueue.add_argument("--producer-execution-key")
    enqueue.add_argument("--telemetry-stride", type=int, default=10)
    enqueue.set_defaults(function=platform_enqueue_run)

    flush = platform_sub.add_parser("flush")
    flush.add_argument("--base-url", required=True)
    flush.add_argument("--token")
    flush.add_argument("--outbox", type=Path, required=True)
    flush.add_argument("--limit", type=int, default=100)
    flush.set_defaults(function=platform_flush)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.function(args))
    except (ValueError, RuntimeError, FileNotFoundError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
