"""Data-only run artifacts. This module never creates reports or plots."""

from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .trajectory import sha256_file

ALLOWED_SUFFIXES = {".csv", ".jsonl", ".log", ".json", ".yaml", ".yml"}


class RunArtifacts:
    def __init__(self, root: Path, run_id: str, config_path: Path) -> None:
        self.directory = root.expanduser().resolve() / run_id
        self.directory.mkdir(parents=True, exist_ok=False)
        self.telemetry_path = self.directory / "telemetry.csv"
        self.events_path = self.directory / "events.jsonl"
        self.native_log_path = self.directory / "native.log"
        self.config_snapshot_path = self.directory / f"config.snapshot{config_path.suffix or '.yaml'}"
        shutil.copyfile(config_path, self.config_snapshot_path)
        self.native_log_path.touch()
        self._events = self.events_path.open("a", encoding="utf-8")
        self._csv = None
        self._writer = None
        self.started_at = datetime.now(timezone.utc).isoformat()

    def start_telemetry(self, fieldnames: list[str]) -> None:
        self._csv = self.telemetry_path.open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(self._csv, fieldnames=fieldnames)
        self._writer.writeheader()

    def write_telemetry(self, row: dict[str, Any]) -> None:
        if self._writer is None:
            raise RuntimeError("telemetry writer has not been started")
        self._writer.writerow(row)

    def write_event(self, event: dict[str, Any]) -> None:
        payload = {
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **event,
        }
        self._events.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
        self._events.flush()

    def finalize(self, *, status: str, extra: dict[str, Any] | None = None) -> Path:
        if self._csv is not None:
            self._csv.close()
            self._csv = None
        self._events.close()
        files = []
        for path in sorted(self.directory.iterdir()):
            if path.name in {"manifest.json", "metadata.json"}:
                continue
            if path.suffix not in ALLOWED_SUFFIXES:
                raise RuntimeError(f"disallowed artifact type: {path.name}")
            files.append(
                {"name": path.name, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
            )
        metadata = {
            "run_id": self.directory.name,
            "status": status,
            "started_at": self.started_at,
            "ended_at": datetime.now(timezone.utc).isoformat(),
            **(extra or {}),
        }
        metadata_path = self.directory / "metadata.json"
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        files.append(
            {
                "name": metadata_path.name,
                "size_bytes": metadata_path.stat().st_size,
                "sha256": sha256_file(metadata_path),
            }
        )
        manifest_path = self.directory / "manifest.json"
        manifest_path.write_text(
            json.dumps({"run_id": self.directory.name, "files": files}, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        return manifest_path
