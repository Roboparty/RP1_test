"""Platform CLI commands."""

from __future__ import annotations

import argparse
import json
import os

from ..api import IngestionClient
from ..outbox import SQLiteOutbox
from ..run_ingestion import enqueue_run


def platform_flush(args: argparse.Namespace) -> int:
    token = args.token or os.environ.get("RP1_TEST_API_TOKEN", "")
    if not token.strip():
        raise ValueError("--token or RP1_TEST_API_TOKEN is required")
    client = IngestionClient(args.base_url, token, SQLiteOutbox(args.outbox))
    sent, failed = client.flush(limit=args.limit)
    print(json.dumps({"sent": sent, "failed": failed}))
    return 1 if failed else 0


def platform_enqueue_run(args: argparse.Namespace) -> int:
    result = enqueue_run(
        args.run_dir,
        SQLiteOutbox(args.outbox),
        producer_execution_key=args.producer_execution_key,
        telemetry_stride=args.telemetry_stride,
    )
    print(json.dumps(result, sort_keys=True))
    return 0
