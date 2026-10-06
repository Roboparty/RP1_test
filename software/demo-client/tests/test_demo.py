import csv
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from rp1_test.api import IngestionClient
from rp1_test.outbox import SQLiteOutbox
from rp1_test.run_ingestion import enqueue_run

from rp1_demo.cli import main
from rp1_demo.demo import generate_demo

CONFIG = Path(__file__).parents[1] / "config/demo.yaml"


def context():
    ids = (
        "campaign_id",
        "cycle_id",
        "segment_id",
        "asset_id",
        "configuration_id",
        "test_case_version_id",
        "station_id",
    )
    return {
        **{name: f"00000000-0000-4000-8000-{index:012d}" for index, name in enumerate(ids, 1)},
        "api_key": "DO-NOT-COPY-CREDENTIAL-TO-ARTIFACTS",
    }


def test_synthetic_run_has_aligned_events_and_excludes_pauses(tmp_path):
    run_dir = generate_demo(
        CONFIG, tmp_path, "DEMO-one", duration=12, pauses=[(4, 2)], context=context()
    )
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["status"] == "synthetic"
    assert metadata["test_outcome"] == "INCONCLUSIVE"
    assert metadata["source_kind"] == "synthetic"
    assert metadata["active_seconds"] == 10
    assert metadata["confirmed_active_intervals"] == [
        {"start_s": 0, "end_s": 4},
        {"start_s": 6, "end_s": 12},
    ]
    started = datetime.fromisoformat(metadata["started_at"])
    assert datetime.fromisoformat(metadata["ended_at"]) - started == timedelta(seconds=12)
    events = [json.loads(line) for line in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [event["elapsed_s"] for event in events] == sorted(
        event["elapsed_s"] for event in events
    )
    for event in events:
        assert datetime.fromisoformat(event["occurred_at"]) == started + timedelta(
            seconds=event["elapsed_s"]
        )
    with (run_dir / "telemetry.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2 * (12 * 20 + 1)
    assert {row["source_kind"] for row in rows} == {"synthetic"}
    assert {row["phase"] for row in rows} == {"active", "paused", "finished"}
    assert all(float(row["spd_rad_s"]) == 0 for row in rows if row["phase"] == "paused")
    assert (
        "DO-NOT-COPY-CREDENTIAL-TO-ARTIFACTS" not in (run_dir / "config.snapshot.yaml").read_text()
    )
    assert "rp1_test.adapters.roboparty_motors" not in sys.modules


def test_offline_queue_remains_persistent_and_idempotent(tmp_path):
    run_dir = generate_demo(
        CONFIG, tmp_path / "runs", "DEMO-queued", duration=12, pauses=[(4, 2)], context=context()
    )
    db = tmp_path / "outbox.sqlite3"
    first = enqueue_run(run_dir, SQLiteOutbox(db), telemetry_stride=1)
    second = enqueue_run(run_dir, SQLiteOutbox(db), telemetry_stride=1)
    assert first["idempotency_keys"] == second["idempotency_keys"]
    box = SQLiteOutbox(db)
    original_count = box.count()
    assert original_count == first["queued_requests"]
    finish = box.pending()[-1].payload
    assert finish["active_seconds"] == 10
    assert finish["outcome"] == "INCONCLUSIVE"
    assert finish["source_status"] == "blocked"

    def offline(item):
        raise ConnectionError("offline demonstration")

    assert box.flush(offline) == (0, 1)
    # Reopen SQLite: failed and dependent requests remain, retry time is persisted.
    reopened = SQLiteOutbox(db)
    assert reopened.count() == original_count
    assert reopened.pending() == []  # Later requests cannot bypass create's backoff.
    assert reopened.pending(now=float("inf"))[0].attempts == 1
    assert reopened.pending(now=float("inf"))[0].path.endswith("/executions")


def test_missing_context_fails_before_queueing(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["demo", "--config", str(CONFIG), "--output", str(tmp_path), "--enqueue"])
    assert exc.value.code == 2
    assert "--context" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())
    run_dir = generate_demo(CONFIG, tmp_path, "DEMO-offline", duration=2)
    box = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    with pytest.raises(ValueError, match="execution_context"):
        enqueue_run(run_dir, box)
    assert box.count() == 0


def test_invalid_context_and_pauses_fail_without_run(tmp_path):
    invalid = context()
    invalid["campaign_id"] = "not-a-public-id"
    with pytest.raises(ValueError, match="campaign_id"):
        generate_demo(CONFIG, tmp_path, "DEMO-invalid", context=invalid)
    for pauses in ([(2, 3), (4, 1)], [(-1, 1)], [(11, 2)]):
        with pytest.raises(ValueError, match="pauses"):
            generate_demo(CONFIG, tmp_path, "DEMO-invalid", duration=12, pauses=pauses)
    assert not list(tmp_path.iterdir())


def test_upload_keeps_queue_on_network_failure(tmp_path, monkeypatch, capsys):
    context_file = tmp_path / "context.json"
    context_file.write_text(json.dumps(context()))
    monkeypatch.delenv("RP1_TEST_API_TOKEN", raising=False)

    def disconnected(self, item):
        assert self.token == context()["api_key"]
        raise ConnectionError("connection unavailable")

    monkeypatch.setattr(IngestionClient, "send", disconnected)
    outbox = tmp_path / "outbox.sqlite3"
    assert (
        main(
            [
                "demo",
                "--config",
                str(CONFIG),
                "--context",
                str(context_file),
                "--output",
                str(tmp_path / "runs"),
                "--run-id",
                "DEMO-upload",
                "--duration",
                "12",
                "--pause",
                "4:2",
                "--outbox",
                str(outbox),
                "--upload",
                "--base-url",
                "http://unavailable.invalid",
            ]
        )
        == 1
    )
    output = capsys.readouterr().out
    assert context()["api_key"] not in output
    result = json.loads(output)
    assert result["upload"]["failed"] == 1
    assert result["upload"]["pending"] == result["queue"]["queued_requests"]
    assert SQLiteOutbox(outbox).count() > 0
