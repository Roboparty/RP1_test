import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from rp1_test.api import BY_PRODUCER_ROOT, EXECUTIONS_PATH
from rp1_test.artifacts import RunArtifacts
from rp1_test.outbox import SQLiteOutbox
from rp1_test.run_ingestion import _convert_events, _execution_payload, enqueue_run

SOFTWARE_ROOT = Path(__file__).parents[2]
SCHEMA_PATH = SOFTWARE_ROOT / "service/app/schemas/ingestion.py"
CATALOG_PATH = SOFTWARE_ROOT / "database/sql/020_edge_collector_ingestion.sql"
METRIC_CATALOG_PATH = SOFTWARE_ROOT / "database/sql/018_execution_joint_telemetry.sql"


def _load_platform_schema():
    name = "rp1_platform_ingestion_contract"
    spec = importlib.util.spec_from_file_location(name, SCHEMA_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _config() -> dict:
    return {
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
                "stage_code": "STATIC-CONTRACT",
                "stage_name": "Static contract test",
                "source_metadata": {"collector": "rp1-test"},
            },
        },
    }


def _make_run(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_config()), encoding="utf-8")
    artifacts = RunArtifacts(tmp_path / "runs", "contract-run-1", config_path)
    artifacts.start_telemetry(
        [
            "time_s",
            "joint_name",
            "motor_index",
            "cmd_pos_rad",
            "pos_rad",
            "spd_rad_s",
            "torque_nm",
            "temp_c",
            "error_id",
        ]
    )
    for index in range(4):
        artifacts.write_telemetry(
            {
                "time_s": index * 0.01,
                "joint_name": "j0",
                "motor_index": 0,
                "cmd_pos_rad": 0.1,
                "pos_rad": 0.09,
                "spd_rad_s": 0.01,
                "torque_nm": 0.2,
                "temp_c": 30.0,
                "error_id": 0,
            }
        )
    artifacts.write_event({"type": "WATCHDOG_OK", "elapsed_s": 0.02})
    return artifacts.finalize(status="completed").parent


def test_enqueue_payloads_validate_against_real_platform_models(tmp_path):
    assert EXECUTIONS_PATH == "/api/v1/ingestion/executions"
    assert BY_PRODUCER_ROOT == "/api/v1/ingestion/executions/by-producer"
    schema = _load_platform_schema()
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    enqueue_run(_make_run(tmp_path), outbox, telemetry_stride=1)
    model_by_operation = {
        "events": schema.EventBatchRequest,
        "telemetry": schema.TelemetryBatchRequest,
        "artifacts": schema.ArtifactMetadataRequest,
        "finish": schema.FinishExecutionRequest,
    }
    stage_id = "22222222-2222-4222-8222-222222222222"
    telemetry_series = []
    for item in outbox.pending(limit=100):
        payload = dict(item.payload)
        if item.path == EXECUTIONS_PATH:
            model = schema.ExecutionRegisterRequest
        else:
            assert item.path.startswith(f"{BY_PRODUCER_ROOT}/contract-run-1/")
            operation = item.path.rsplit("/", 1)[-1]
            model = model_by_operation[operation]
            if operation == "telemetry":
                assert "stage_id" not in payload
                payload["stage_id"] = stage_id
                telemetry_series.extend(payload["series"])
        assert set(payload).issubset(model.model_fields)
        model.model_validate(payload)
        assert "schema_version" not in payload
        assert "producer_execution_key" not in payload
    finish = outbox.pending(limit=100)[-1].payload
    create = outbox.pending(limit=100)[0].payload
    wall = (
        datetime.fromisoformat(finish["finished_at"])
        - datetime.fromisoformat(create["started_at"])
    ).total_seconds()
    assert finish["active_seconds"] <= wall
    by_metric = {item["metric_code"]: item for item in telemetry_series}
    assert by_metric["JOINT-TORQUE"]["canonical_unit"] == "N·m"
    assert by_metric["JOINT-TORQUE"]["points"][0]["value"] == pytest.approx(0.2)
    assert by_metric["JOINT-TEMPERATURE"]["canonical_unit"] == "°C"
    assert by_metric["JOINT-TEMPERATURE"]["points"][0]["value"] == pytest.approx(30.0)
    error = by_metric["JOINT-TRACKING-ERROR"]
    assert error["canonical_unit"] == "deg"
    assert error["points"][0]["value"] == pytest.approx(-0.01 * 180 / 3.141592653589793)
    assert error["raw_data_reference"]["expression"] == "pos_rad - cmd_pos_rad"
    assert error["raw_data_reference"]["source_columns"] == ["pos_rad", "cmd_pos_rad"]


def test_subject_codes_match_real_catalog_and_example():
    catalog = CATALOG_PATH.read_text(encoding="utf-8")
    expected = [
        "SARM-SHOULDER-PITCH",
        "SARM-SHOULDER-ROLL",
        "SARM-SHOULDER-YAW",
        "SARM-ELBOW-PITCH",
        "SARM-WRIST-YAW",
        "SARM-WRIST-PITCH",
        "SARM-WRIST-ROLL",
    ]
    for code in expected:
        assert f"'{code}'" in catalog
    config_path = Path(__file__).parents[1] / "config/rp1_test.example.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert [
        item["telemetry_subject_code"] for item in config["joint_mappings"]
    ] == expected
    metric_catalog = METRIC_CATALOG_PATH.read_text(encoding="utf-8")
    for metric_code in (
        "JOINT-TARGET-POSITION",
        "JOINT-ACTUAL-POSITION",
        "JOINT-TRACKING-ERROR",
        "JOINT-VELOCITY",
        "JOINT-TORQUE",
        "JOINT-TEMPERATURE",
    ):
        assert f"'{metric_code}'" in metric_catalog


def test_events_without_absolute_time_are_anchored_to_run_start(tmp_path):
    del tmp_path
    started = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    event = _convert_events(
        [{"type": "WATCHDOG_OK", "elapsed_s": 1.25}], {}, started
    )[0]
    assert datetime.fromisoformat(event["occurred_at"]) == started + timedelta(
        seconds=1.25
    )
    assert event["clock_quality"] == "PARTIAL"
    assert event["producer_event_key"].startswith("rp1-event-")
    assert set(event) == {
        "producer_event_key",
        "event_type",
        "occurred_at",
        "payload",
        "clock_quality",
        "data_quality",
    }


def test_execution_context_fails_fast_on_missing_or_invalid_values():
    metadata = {"started_at": "2026-01-02T03:04:05+00:00"}
    with pytest.raises(ValueError, match="execution_context"):
        _execution_payload({}, metadata, "run-1")
    config = _config()
    config["platform"]["execution_context"]["campaign_id"] = "replace-me"
    with pytest.raises(ValueError, match="campaign_id"):
        _execution_payload(config, metadata, "run-1")
