import json

import yaml

from rp1_test.artifacts import RunArtifacts
from rp1_test.outbox import SQLiteOutbox
from rp1_test.run_ingestion import enqueue_run


def test_run_artifacts_are_data_only(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("limb: test\n", encoding="utf-8")
    artifacts = RunArtifacts(tmp_path / "runs", "run-1", config)
    artifacts.start_telemetry(["time_s", "value"])
    artifacts.write_telemetry({"time_s": 0.0, "value": 1.0})
    artifacts.write_event({"type": "start"})
    manifest_path = artifacts.finalize(status="completed")
    names = {path.name for path in manifest_path.parent.iterdir()}
    assert names == {
        "telemetry.csv",
        "events.jsonl",
        "native.log",
        "config.snapshot.yaml",
        "metadata.json",
        "manifest.json",
    }
    manifest = json.loads(manifest_path.read_text())
    assert manifest["run_id"] == "run-1"


def test_finalized_run_is_queued_in_stable_order(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "joint_mappings": [
                    {
                        "joint_name": "j0",
                        "motor_index": 0,
                        "telemetry_subject_code": "SARM-SHOULDER-PITCH",
                        "target_part_code": "SARM",
                    }
                ],
                "platform": {
                    "metric_version": "1.0.0",
                    "execution_context": {
                        "campaign_id": "00000000-0000-4000-8000-000000000001",
                        "cycle_id": "00000000-0000-4000-8000-000000000002",
                        "segment_id": "00000000-0000-4000-8000-000000000003",
                        "asset_id": "00000000-0000-4000-8000-000000000004",
                        "configuration_id": "00000000-0000-4000-8000-000000000005",
                        "test_case_version_id": "00000000-0000-4000-8000-000000000006",
                        "station_id": "00000000-0000-4000-8000-000000000007",
                        "started_at": "$RUN_STARTED_AT",
                        "source_status": "running",
                        "stage_code": "TEST",
                        "stage_name": "Test stage",
                        "source_metadata": {"collector": "test"},
                    },
                },
                "telemetry_gaps": {
                    "bus_voltage": "unavailable",
                    "vibration": "unavailable",
                },
            }
        ),
        encoding="utf-8",
    )
    artifacts = RunArtifacts(tmp_path / "runs", "run-2", config)
    fields = [
        "time_s",
        "motor_index",
        "cmd_pos_rad",
        "pos_rad",
        "spd_rad_s",
        "torque_nm",
        "temp_c",
        "error_id",
    ]
    artifacts.start_telemetry(fields)
    for index in range(3):
        artifacts.write_telemetry(
            {
                "time_s": index * 0.01,
                "motor_index": 0,
                "cmd_pos_rad": 0.1,
                "pos_rad": 0.1,
                "spd_rad_s": 0.0,
                "torque_nm": float("nan"),
                "temp_c": 35.0,
                "error_id": 0,
            }
        )
    artifacts.write_event({"type": "start"})
    run_dir = artifacts.finalize(status="completed").parent
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    first = enqueue_run(run_dir, outbox, telemetry_stride=2)
    second = enqueue_run(run_dir, outbox, telemetry_stride=2)
    assert first["idempotency_keys"] == second["idempotency_keys"]
    assert outbox.count() == 10
    pending = outbox.pending()
    assert [item.path.rsplit("/", 1)[-1] for item in pending] == [
        "executions",
        "events",
        "telemetry",
        "artifacts",
        "artifacts",
        "artifacts",
        "artifacts",
        "artifacts",
        "artifacts",
        "finish",
    ]
    telemetry = pending[2].payload
    assert set(telemetry) == {"producer_key", "series"}
    assert [item["metric_code"] for item in telemetry["series"]] == [
        "JOINT-TARGET-POSITION",
        "JOINT-ACTUAL-POSITION",
        "JOINT-TRACKING-ERROR",
        "JOINT-VELOCITY",
        "JOINT-TEMPERATURE",
    ]
    assert all(item["sampling_interval_ms"] >= 10 for item in telemetry["series"])
    assert all(
        [point["sample_index"] for point in item["points"]]
        == list(range(len(item["points"])))
        for item in telemetry["series"]
    )
    assert all("samples" not in item["raw_data_reference"] for item in telemetry["series"])
    assert pending[3].payload["availability_status"] == "MISSING"
