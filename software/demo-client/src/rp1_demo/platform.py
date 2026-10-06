"""Public context and credentials for the existing rp1_test ingestion client."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from rp1_test.api import IngestionClient
from rp1_test.outbox import SQLiteOutbox


def read_context(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("context must contain a JSON object")
    return payload


def flush_queue(
    base_url: str,
    outbox_path: Path,
    *,
    token_env: str = "RP1_TEST_API_TOKEN",
    token_file: Path | None = None,
    context_path: Path | None = None,
    limit: int = 1000,
) -> dict[str, int]:
    token = os.environ.get(token_env, "").strip()
    if token_file is not None:
        token = token_file.read_text(encoding="utf-8").strip()
    if not token and context_path is not None:
        context = read_context(context_path) or {}
        token = str(context.get("api_key", "")).strip()
    if not token:
        raise ValueError(
            "Bearer token is required: use --token-file, --token-env or context.api_key"
        )
    outbox = SQLiteOutbox(outbox_path)
    sent, failed = IngestionClient(base_url, token, outbox).flush(limit=limit)
    return {"sent": sent, "failed": failed, "pending": outbox.count()}
