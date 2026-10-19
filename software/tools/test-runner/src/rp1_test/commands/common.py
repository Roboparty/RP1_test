"""Common CLI commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import yaml

from ..mapping import capability_gaps, load_joint_mappings
from ..simulation import resolve_model_path
from ..validation import load_safety_limits


def load_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"config not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("config root must be a mapping")
    load_joint_mappings(payload)
    load_safety_limits(payload)
    capability_gaps(payload)
    return payload


def _params(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    if value.startswith("@"):
        result = json.loads(Path(value[1:]).read_text(encoding="utf-8"))
    else:
        result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("--params must decode to a JSON object")
    return result


def _model_from_args(args: argparse.Namespace, config: dict[str, Any]) -> Path | None:
    if getattr(args, "model", None):
        return Path(args.model).expanduser().resolve()
    configured = str(config.get("simulation", {}).get("model", "")).strip()
    if configured:
        return (args.config.parent / configured).resolve()
    return resolve_model_path(None)
