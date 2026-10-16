from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.ingestion import EventBatchRequest, TelemetryBatchRequest
from app.core.config import Settings
from app.core.errors import ApiError
from app.repositories.ingestion import IngestionRepository
from app.services.auth import ServiceCredentialService
from app.services.ingestion import IngestionService


def _series(unit: str = "deg") -> dict:
    started = datetime.now(timezone.utc)
    return {
        "producer_series_key": "series-001",
        "metric_code": "JOINT-ACTUAL-POSITION",
        "metric_version": "1.0.0",
        "subject_code": "SYS-HIP-PITCH-L",
        "canonical_unit": unit,
        "series_kind": "MOTION_CYCLE",
        "cycle_index": 1,
        "started_at": started,
        "ended_at": started + timedelta(milliseconds=20),
        "sampling_interval_ms": 10,
        "downsample_method": "UNIFORM",
        "points": [
            {
                "sample_index": 0,
                "observed_at": started,
                "value": 0.0,
            },
            {
                "sample_index": 1,
                "observed_at": started + timedelta(milliseconds=10),
                "value": 1.0,
            },
        ],
    }


def test_telemetry_contract_accepts_normalized_degree_series():
    payload = TelemetryBatchRequest(
        producer_key="batch-0001",
        stage_id="00000000-0000-7000-8000-000000000001",
        series=[_series()],
    )
    assert payload.series[0].canonical_unit == "deg"
    assert payload.series[0].downsample_method == "UNIFORM"


def test_telemetry_contract_rejects_raw_rate_and_non_contiguous_samples():
    series = _series()
    series["sampling_interval_ms"] = 1
    series["points"][1]["sample_index"] = 2
    with pytest.raises(ValidationError):
        TelemetryBatchRequest(
            producer_key="batch-0002",
            stage_id="00000000-0000-7000-8000-000000000001",
            series=[series],
        )


def test_telemetry_contract_does_not_offer_rad_conversion_method():
    series = _series("rad")
    payload = TelemetryBatchRequest(
        producer_key="batch-0003",
        stage_id="00000000-0000-7000-8000-000000000001",
        series=[series],
    )
    # Metric-specific canonical-unit equality is checked against the published
    # DB metric in the repository; the API has no conversion flag or factor.
    assert "conversion" not in payload.series[0].model_fields_set


def test_repository_rejects_rad_for_degree_metric():
    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

        def mappings(self):
            return self

        def first(self):
            return self.value

    class Session:
        def __init__(self):
            self.calls = 0

        def execute(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return Result(101)
            return Result({"id": 201, "canonical_unit": "deg"})

    class Repository(IngestionRepository):
        def _source(self):
            return {"id": 1}

        def _execution(self, _execution_id):
            return {
                "id": 2,
                "execution_id": 2,
                "campaign_id": 3,
                "status": "RUNNING",
            }

        def _claim(self, **_kwargs):
            return 4, None

        def _start_batch(self, **_kwargs):
            return 5

    payload = TelemetryBatchRequest(
        producer_key="batch-rad",
        stage_id="00000000-0000-7000-8000-000000000001",
        series=[_series("rad")],
    )
    with pytest.raises(ApiError) as mismatch:
        Repository(Session()).ingest_telemetry(
            uuid4(), payload, "idempotency-rad"
        )
    assert mismatch.value.status_code == 422
    assert mismatch.value.code == "unit_mismatch"


def test_service_provisioning_only_passes_hmac_hash_to_repository():
    class Repository:
        def __init__(self):
            self.values = None

        def provision_service_credential(self, **values):
            self.values = values
            return {
                "service_public_id": uuid4(),
                "credential_public_id": uuid4(),
                "source_public_id": uuid4(),
            }

    repository = Repository()
    grant = ServiceCredentialService(
        Settings(
            app_env="test",
            seed_admin_username=None,
            seed_admin_password=None,
            service_api_key_hmac_secret="unit-test-pepper",
        ),
        repository,
    ).provision(
        username="collector",
        display_name="Collector",
        source_code="EDGE-COLLECTOR",
        campaign_ids=[uuid4()],
        expires_at=None,
        request_id="test",
        source_ip="127.0.0.1",
    )
    assert grant.api_key.startswith(f"rp1svc_{grant.key_id}_")
    assert repository.values is not None
    assert len(repository.values["secret_hash"]) == 64
    assert repository.values["secret_hash"] not in grant.api_key


def test_producer_execution_lookup_is_always_bound_to_current_source():
    execution_id = uuid4()

    class Result:
        def scalar_one_or_none(self):
            return execution_id

    class Session:
        def __init__(self):
            self.statement = ""
            self.parameters = {}

        def execute(self, statement, parameters):
            self.statement = str(statement)
            self.parameters = parameters
            return Result()

    session = Session()
    resolved = IngestionRepository(session).execution_id_by_producer(
        "offline-run-001"
    )
    assert resolved == execution_id
    assert session.parameters == {
        "producer_execution_key": "offline-run-001"
    }
    assert (
        "execution.source_id =\n"
        "                      iam.current_ingestion_source_id()"
        in session.statement
    )


def test_by_producer_service_preserves_request_idempotency_key():
    execution_id = uuid4()
    event_payload = EventBatchRequest(
        producer_key="offline-event-batch-001",
        events=[
            {
                "producer_event_key": "offline-event-001",
                "event_type": "EDGE_EVENT",
                "occurred_at": datetime.now(timezone.utc),
            }
        ],
    )

    class Repository:
        def __init__(self):
            self.call = None

        def execution_id_by_producer(self, producer_execution_key):
            assert producer_execution_key == "offline-run-001"
            return execution_id

        def ingest_events(self, *args):
            self.call = args
            return "accepted"

    repository = Repository()
    result = IngestionService(repository).events_by_producer(
        "offline-run-001",
        event_payload,
        "idempotency-offline-events-001",
    )
    assert result == "accepted"
    assert repository.call == (
        execution_id,
        event_payload,
        "idempotency-offline-events-001",
    )


def test_edge_subject_seed_codes_and_display_orders_are_unique():
    sql_path = (
        Path(__file__).resolve().parents[2]
        / "database"
        / "sql"
        / "020_edge_collector_ingestion.sql"
    )
    sql = sql_path.read_text(encoding="utf-8")
    seed_block = sql.split(
        "-- EDGE_COLLECTOR_SUBJECT_SEEDS_BEGIN", 1
    )[1].split("-- EDGE_COLLECTOR_SUBJECT_SEEDS_END", 1)[0]
    rows = re.findall(
        r"\('([^']+)',\s*'[^']+',\s*'JOINT',\s*"
        r"'(SARM|SLEG|UPPER|LOWER)',\s*(\d+)\)",
        seed_block,
    )
    expected_counts = {"SARM": 7, "SLEG": 6, "UPPER": 12, "LOWER": 14}
    assert len(rows) == sum(expected_counts.values())
    assert len({code for code, _, _ in rows}) == len(rows)
    assert len({(part, int(order)) for _, part, order in rows}) == len(rows)
    assert all(
        re.fullmatch(r"[A-Z0-9]+(?:-[A-Z0-9]+)*", code)
        for code, _, _ in rows
    )
    for part, expected_count in expected_counts.items():
        orders = sorted(
            int(order) for _, row_part, order in rows if row_part == part
        )
        assert orders == list(range(1, expected_count + 1))
    assert {
        "SARM-SHOULDER-PITCH",
        "SARM-SHOULDER-ROLL",
        "SARM-SHOULDER-YAW",
        "SARM-ELBOW-PITCH",
        "SARM-WRIST-YAW",
        "SARM-WRIST-PITCH",
        "SARM-WRIST-ROLL",
    } == {code for code, part, _ in rows if part == "SARM"}
