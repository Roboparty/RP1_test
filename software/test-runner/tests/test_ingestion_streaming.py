import csv
import gc
import json
import sqlite3
import tracemalloc
from datetime import datetime

import pytest
import yaml

from rp1_test import run_ingestion as ingestion
from rp1_test.outbox import SQLiteOutbox
from rp1_test.trajectory import sha256_file

FIELDS = [
    "time_s",
    "motor_index",
    "cmd_pos_rad",
    "pos_rad",
    "spd_rad_s",
    "torque_nm",
    "temp_c",
    "error_id",
]
STARTED = "2026-01-01T00:00:00+00:00"


def _run(tmp_path, rows, *, mappings=None, events=None):
    run = tmp_path / "run"
    run.mkdir(parents=True)
    config = {
        "joint_mappings": mappings
        or [{"motor_index": 0, "telemetry_subject_code": "SARM-SHOULDER-PITCH"}],
        "platform": {
            "execution_context": {
                **{
                    name: f"00000000-0000-4000-8000-{index:012d}"
                    for index, name in enumerate(
                        (
                            "campaign_id",
                            "cycle_id",
                            "segment_id",
                            "asset_id",
                            "configuration_id",
                            "test_case_version_id",
                            "station_id",
                        ),
                        1,
                    )
                },
                "started_at": "$RUN_STARTED_AT",
                "stage_code": "INGESTION-TEST",
                "stage_name": "Offline ingestion test",
            }
        },
    }
    (run / "config.snapshot.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    (run / "metadata.json").write_text(
        json.dumps(
            {
                "run_id": "streaming-test",
                "started_at": STARTED,
                "ended_at": "2026-01-02T00:00:00+00:00",
                "status": "completed",
                "source_kind": "synthetic",
            }
        ),
        encoding="utf-8",
    )
    with (run / "telemetry.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(FIELDS)
        writer.writerows(rows)
    with (run / "events.jsonl").open("w", encoding="utf-8") as handle:
        for event in events or []:
            handle.write(json.dumps(event) + "\n")
    (run / "native.log").write_text("", encoding="utf-8")
    _manifest(run)
    return run, config


def _manifest(run):
    files = [
        {"name": path.name, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)}
        for path in sorted(run.iterdir())
        if path.name != "manifest.json"
    ]
    (run / "manifest.json").write_text(
        json.dumps({"run_id": "streaming-test", "files": files}), encoding="utf-8"
    )


def _rows(count):
    for index in range(count - 1, -1, -1):
        yield [index * 0.02, 0, 0.1, 0.09, "", "", "", 0]


def test_streamed_series_and_event_batches_equal_legacy_conversion(tmp_path):
    mappings = [
        {"motor_index": 2, "telemetry_subject_code": "SARM-SHOULDER-ROLL"},
        {"motor_index": 0, "telemetry_subject_code": "SARM-SHOULDER-PITCH"},
    ]

    def rows():
        # Unsorted, interleaved motors and equal timestamps exercise stable
        # ordering; missing and nonfinite values exercise per-metric filtering.
        for index in range(12029, -1, -1):
            for motor in (2, 0):
                yield [
                    (index // 2) * 0.006,
                    motor,
                    "" if index % 17 == 0 else 0.1 + index * 0.000001,
                    "nan" if index % 13 == 0 else 0.09 + motor * 0.001,
                    0.02,
                    "inf" if index % 11 == 0 else 0.3,
                    30.0,
                    0,
                ]

    raw_events = [{"type": "SAMPLE", "elapsed_s": index * 0.1} for index in range(1003)]
    run, config = _run(tmp_path, rows(), mappings=mappings, events=raw_events)
    started = datetime.fromisoformat(STARTED)
    expected = ingestion._telemetry_series(
        ingestion._read_telemetry(run / "telemetry.csv"),
        mappings,
        producer_execution_key="streaming-test",
        started_at=started,
        telemetry_path=run / "telemetry.csv",
        requested_stride=1,
        metric_version="1.0.0",
    )
    expected_events = ingestion._convert_events(raw_events, {}, started)
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    result = ingestion.enqueue_run(run, outbox, telemetry_stride=1)
    items = outbox.pending(limit=100)
    actual = [
        series
        for item in items
        if item.path.endswith("/telemetry")
        for series in item.payload["series"]
    ]
    actual_events = [
        event for item in items if item.path.endswith("/events") for event in item.payload["events"]
    ]
    assert actual == expected
    assert actual_events == expected_events
    assert len(expected) > ingestion.MAX_SERIES_PER_BATCH
    assert result["telemetry_rows"] == 24060
    assert result["telemetry_series"] == len(expected)
    assert result["telemetry_batches"] == 2
    assert result["event_count"] == 1003
    for batch_index, item in enumerate(item for item in items if item.path.endswith("/telemetry")):
        assert item.payload["producer_key"] == ingestion._stable_key(
            f"telemetry-{batch_index}",
            {"series": expected[batch_index * 20 : (batch_index + 1) * 20]},
        )
    replay = ingestion.enqueue_run(run, outbox, telemetry_stride=1)
    assert replay == result
    assert outbox.count() == result["queued_requests"]


def test_production_streams_and_hashes_telemetry_once(tmp_path, monkeypatch):
    run, _ = _run(tmp_path, _rows(10003))
    calls = []
    real_hash = ingestion.sha256_file

    def hashed(path):
        calls.append(path.name)
        return real_hash(path)

    def forbidden(*args, **kwargs):
        pytest.fail("production ingestion must not materialize the complete input")

    monkeypatch.setattr(ingestion, "sha256_file", hashed)
    for name in ("_read_telemetry", "_telemetry_series", "_read_events"):
        monkeypatch.setattr(ingestion, name, forbidden)
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    result = ingestion.enqueue_run(run, outbox, telemetry_stride=1)
    assert calls.count("telemetry.csv") == 1
    assert result["telemetry_series"] == 9
    assert result["event_count"] == 1  # Empty events retain RUN_FINALIZED fallback.
    for item in outbox.pending():
        if item.path.endswith("/telemetry"):
            assert len(item.payload["series"]) <= 20
            assert all(len(series["points"]) <= 5000 for series in item.payload["series"])


@pytest.mark.parametrize("bad_input", ["late-event", "late-csv", "missing-time", "finish"])
def test_invalid_input_never_partially_publishes_a_run(tmp_path, bad_input):
    run, _ = _run(
        tmp_path,
        _rows(12),
        events=({"type": "SAMPLE", "elapsed_s": index * 0.1} for index in range(1001)),
    )
    if bad_input == "late-event":
        with (run / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"type":""}\n')
    elif bad_input in {"late-csv", "missing-time"}:
        with (run / "telemetry.csv").open("a", encoding="utf-8") as handle:
            time = "oops" if bad_input == "late-csv" else "nan"
            handle.write(f"{time},0,0.1,0.09,,,,0\n")
    else:
        metadata = json.loads((run / "metadata.json").read_text(encoding="utf-8"))
        metadata["termination_kind"] = "INVALID-TERMINATION"
        (run / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    _manifest(run)
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    seed = outbox.enqueue("POST", "/existing", {"preserved": True}, idempotency_key="existing")
    with pytest.raises(ValueError):
        ingestion.enqueue_run(run, outbox, telemetry_stride=1)
    assert [(item.idempotency_key, item.payload) for item in outbox.pending()] == [
        (seed, {"preserved": True})
    ]


def test_atomic_publish_rolls_back_when_destination_insert_fails(tmp_path):
    run, _ = _run(tmp_path, _rows(12))
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue("POST", "/existing", {"preserved": True}, idempotency_key="existing")
    with sqlite3.connect(outbox.path) as db:
        db.execute("""CREATE TRIGGER reject_finish BEFORE INSERT ON outbox
                    WHEN NEW.path LIKE '%/finish'
                    BEGIN SELECT RAISE(ABORT, 'test publish failure'); END""")
    with pytest.raises(sqlite3.IntegrityError, match="test publish failure"):
        ingestion.enqueue_run(run, outbox, telemetry_stride=1)
    assert [item.idempotency_key for item in outbox.pending()] == ["existing"]


def test_memory_growth_depends_on_batch_limit_not_csv_length(
    tmp_path, monkeypatch, record_property
):
    # Smaller batch limits keep this regression fast. The production 5000/20
    # boundaries and multi-chunk payload equivalence are checked above.
    monkeypatch.setattr(ingestion, "MAX_POINTS_PER_SERIES", 128)
    monkeypatch.setattr(ingestion, "MAX_SERIES_PER_BATCH", 2)

    def peak_for(count):
        root = tmp_path / str(count)
        run, _ = _run(root, _rows(count))
        outbox = SQLiteOutbox(root / "outbox.sqlite3")
        gc.collect()
        tracemalloc.start()
        try:
            result = ingestion.enqueue_run(run, outbox, telemetry_stride=1)
            return tracemalloc.get_traced_memory()[1], result["queued_requests"]
        finally:
            tracemalloc.stop()

    small, small_requests = peak_for(6000)
    large, large_requests = peak_for(24000)
    record_property("small_peak_bytes", small)
    record_property("large_peak_bytes", large)
    record_property("small_requests", small_requests)
    record_property("large_requests", large_requests)
    assert large_requests > small_requests * 3
    # The public response still returns one key per request; that small O(n)
    # list is intentional. Complete telemetry/series lists would grow ~4x.
    assert large < small * 2
