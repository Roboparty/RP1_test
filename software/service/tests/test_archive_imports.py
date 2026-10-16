"""Historical files cannot fabricate missing machine facts or execution identity."""
from copy import deepcopy
import hashlib
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.schemas.archives import ArchiveCreate, ArchiveImportCreate
from app.services.archive_analysis import retrospective_analysis
from app.services.archive_storage import ArchiveStorage
from app.services.curve_query import CurveProcessor, CurveQueryService
from test_archives import archive_payload


def import_payload(station_id=None, raw=b"time_s,j_pos_rad\n0,0\n1,9\n2,-4\n"):
    return {"filename": "original.csv", "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
            "test_name": "Synthetic historical test", "actual_object": "SYNTHETIC-SAMPLE",
            "station_id": station_id or str(uuid4()), "classification": None}


@pytest.mark.parametrize("extra", ["facts", "execution_uuid", "origin", "active_seconds", "machine_status", "acceptance"])
def test_intake_rejects_client_supplied_machine_facts_and_identity(extra):
    with pytest.raises(ValidationError):
        ArchiveImportCreate.model_validate({**import_payload(), extra: {}})


@pytest.mark.parametrize("field,value", [
    ("filename", "../original.csv"), ("filename", "C:\\raw\\original.csv"),
    ("filename", "a.csv\x00"), ("filename", "a.parquet"), ("size_bytes", 0),
    ("sha256", "a" * 63), ("test_name", "  "), ("actual_object", ""), ("station_id", "RD-01"),
])
def test_intake_rejects_invalid_metadata(field, value):
    with pytest.raises(ValidationError):
        ArchiveImportCreate.model_validate({**import_payload(), field: value})


def test_hmi_and_original_archive_contract_stay_strict():
    for origin in ("HMI", "OTHER", "HISTORY"):
        for field in ("active_seconds", "completed_cycles", "exception_count"):
            payload = archive_payload(origin=origin)
            payload["facts"][field] = None
            with pytest.raises(ValidationError):
                ArchiveCreate.model_validate(payload)


def test_unknown_history_facts_are_not_zero_and_relative_span_is_not_runtime(tmp_path):
    raw = b"time_s,j_pos_rad\n100,0\n101,99\n102,-7\n"
    archive = archive_payload(raw=raw, origin="HISTORY")
    archive["facts"].update(started_at=None, ended_at=None, active_seconds=None, completed_cycles=None,
                            exception_count=None, machine_status="unknown", clock_quality="MISSING", data_quality="MISSING")
    archive["acceptance"] = {"criteria_version": "HISTORY-TEST", "record_rate_hz": 1,
                              "required_active_seconds": 1, "required_cycles": 1}
    storage = ArchiveStorage(tmp_path)
    storage.path(archive["execution_uuid"], "raw.csv").write_bytes(raw)
    archive["manifest"] = CurveProcessor(storage).build(archive)
    archive.update(processing_state="READY", id=str(uuid4()), archive_code="SYNTHETIC")
    analysis = archive["manifest"]["analysis"]
    assert analysis["verdict"] == "blocked"
    assert analysis["quality"]["expected_sample_count"] is None
    assert analysis["quality"]["sample_completeness"] is None
    assert analysis["timing"]["active_seconds"] is None
    assert set(analysis["missing_acquisition_facts"]) == {"active_seconds", "completed_cycles", "exception_count", "machine_status"}
    rule = next(r for r in analysis["rules"] if r["name"] == "execution_completed")
    assert rule["actual"] is None and rule["result"] == "unconfigured"
    assert archive["manifest"]["time_range"] == {"start": 100.0, "end": 102.0}
    points = CurveQueryService(storage).query(archive, ["pos_rad"], [], 100, 102, 50)["series"][0]["points"]
    assert points == [[100.0, 0.0], [101.0, 99.0], [102.0, -7.0]]
    original = deepcopy(archive)
    report = retrospective_analysis(archive, {"id": str(uuid4()), "decision_rules": {
        "archive_acceptance": {"record_rate_hz": 10, "required_active_seconds": 1}}})["report"]
    assert report["verdict"] == "blocked" and report["quality"]["expected_sample_count"] is None
    assert archive == original
    assert hashlib.sha256(storage.path(archive["execution_uuid"], "raw.csv").read_bytes()).hexdigest() == archive["sha256"]
