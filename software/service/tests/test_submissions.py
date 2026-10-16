from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.submissions import SubmissionCreate


def summary():
    end = datetime.now(timezone.utc) - timedelta(minutes=10)
    return {**{k: str(uuid4()) for k in ("execution_uuid", "campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")},
            "started_at": (end - timedelta(seconds=60)).isoformat(), "ended_at": end.isoformat(),
            "active_seconds": 50, "completed_cycles": 3, "source_status": "passed", "data_quality": "VALID",
            "clock_quality": "VALID", "exception_count": 0, "summary": "Synthetic aggregate", "evidence_sha256": "a" * 64}


def test_bounded_summary_rejects_raw_data_and_invalid_timing():
    base = {"summary": summary(), "reason": "Synthetic submission"}
    assert SubmissionCreate(**base).expected_revision == 0
    for field in ("raw_csv", "attachment", "points", "telemetry", "bundle_base64"):
        value = deepcopy(base)
        value["summary"][field] = "synthetic forbidden content"
        with pytest.raises(ValidationError):
            SubmissionCreate(**value)
    for field, invalid in (("active_seconds", 61), ("active_seconds", float("nan")), ("completed_cycles", -1), ("summary", "x" * 501), ("started_at", "2026-10-01T00:00:00")):
        value = deepcopy(base)
        value["summary"][field] = invalid
        with pytest.raises(ValidationError):
            SubmissionCreate(**value)
