"""CLI demo client: commands and dispatch; shared runner owns device execution."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from rp1_test.outbox import SQLiteOutbox
from rp1_test.run_ingestion import enqueue_run

from .demo import generate_demo
from .platform import flush_queue, read_context


def _pause(value: str) -> tuple[float, float]:
    try:
        start, duration = value.split(":")
        return float(start), float(duration)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pause must be START_SECONDS:DURATION_SECONDS") from exc


def _demo(args: argparse.Namespace) -> int:
    context = read_context(args.context)
    if (args.enqueue or args.upload) and context is None:
        raise ValueError(
            "--enqueue/--upload requires --context with seven real platform public IDs"
        )
    run_id = args.run_id or f"DEMO-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    run_dir = generate_demo(
        args.config,
        args.output_dir,
        run_id,
        duration=args.duration,
        sample_rate=args.sample_rate,
        pauses=args.pause,
        context=context,
    )
    result: dict[str, Any] = {"run_dir": str(run_dir), "source_kind": "synthetic"}
    if args.enqueue or args.upload:
        result["queue"] = enqueue_run(run_dir, SQLiteOutbox(args.outbox), telemetry_stride=1)
    exit_code = 0
    if args.upload:
        result["upload"] = _flush_result(args)
        exit_code = 1 if result["upload"]["failed"] else 0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


def _queue(args: argparse.Namespace) -> int:
    result = enqueue_run(args.run_dir, SQLiteOutbox(args.outbox), telemetry_stride=args.stride)
    print(json.dumps(result, sort_keys=True))
    return 0


def _flush_result(args: argparse.Namespace) -> dict[str, int]:
    return flush_queue(
        args.base_url,
        args.outbox,
        token_env=args.token_env,
        token_file=args.token_file,
        context_path=args.context,
        limit=args.limit,
    )


def _flush(args: argparse.Namespace) -> int:
    result = _flush_result(args)
    print(json.dumps(result))
    return 1 if result["failed"] else 0


def _status(args: argparse.Namespace) -> int:
    outbox = SQLiteOutbox(args.outbox)
    print(json.dumps({"pending": outbox.count(), "sent": outbox.count("sent")}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rp1-demo")
    sub = parser.add_subparsers(dest="command", required=True)
    demo = sub.add_parser("demo", help="generate explicitly synthetic artifacts without hardware")
    demo.add_argument("--config", type=Path, required=True)
    demo.add_argument(
        "--output-dir", "--output", dest="output_dir", type=Path, default=Path("runs")
    )
    demo.add_argument("--run-id")
    demo.add_argument("--duration", type=float, default=30.0)
    demo.add_argument("--sample-rate", type=float, default=20.0)
    demo.add_argument("--pause", type=_pause, action="append", default=[])
    demo.add_argument("--context", type=Path)
    demo.add_argument("--enqueue", action="store_true")
    demo.add_argument(
        "--upload", action="store_true", help="enqueue then upload due requests; retains failures"
    )
    demo.add_argument("--base-url", default="http://127.0.0.1:18080")
    demo.add_argument("--token-env", default="RP1_TEST_API_TOKEN")
    demo.add_argument("--token-file", type=Path)
    demo.add_argument("--limit", type=int, default=1000)
    demo.add_argument("--outbox", type=Path, default=Path(".rp1-demo/outbox.sqlite3"))
    demo.set_defaults(function=_demo)
    queue = sub.add_parser("queue", help="validate artifacts and persist ingestion requests")
    queue.add_argument("run_dir", type=Path)
    queue.add_argument("--outbox", type=Path, required=True)
    queue.add_argument("--stride", type=int, default=1)
    queue.set_defaults(function=_queue)
    flush = sub.add_parser("flush", help="retry due outbox requests via ingestion API")
    flush.add_argument("--base-url", required=True)
    flush.add_argument("--outbox", type=Path, required=True)
    flush.add_argument("--token-env", default="RP1_TEST_API_TOKEN")
    flush.add_argument("--token-file", type=Path)
    flush.add_argument("--context", type=Path)
    flush.add_argument("--limit", type=int, default=1000)
    flush.set_defaults(function=_flush)
    status = sub.add_parser("status", help="show persistent queue counts")
    status.add_argument("--outbox", type=Path, required=True)
    status.set_defaults(function=_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.function(args))
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
