from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.repositories.hmi_presence import station_view
from app.schemas.hmi_presence import HmiHeartbeat


def row():
    now = datetime.now(timezone.utc)
    return {
        "connection_id": uuid4(), "installation_id": uuid4(),
        "station_name": "QA station", "bench_id": "QA-01",
        "operator_name": "operator", "operator_display_name": "Operator",
        "online": True, "connected_at": now, "last_seen_at": now,
        "snapshot_at": now, "runs": [{"session_key": "first", "state": "running"}],
        "plc": {"phase": "RUNNING", "driver": "mock", "stale": False},
    }


def test_live_test_and_power_only_have_distinct_states():
    payload = row()
    assert station_view(payload, datetime.now(timezone.utc)).state == "running"
    payload["runs"] = []
    assert station_view(payload, datetime.now(timezone.utc)).state == "powered"


def test_missing_heartbeat_never_displays_a_live_running_test():
    payload = row()
    payload["online"] = False
    view = station_view(payload, datetime.now(timezone.utc))
    assert view.state == "offline"
    assert view.snapshot_fresh is False


def test_old_controller_snapshot_is_unknown_even_with_online_gateway():
    payload = row()
    payload["snapshot_at"] -= timedelta(seconds=21)
    assert station_view(payload, datetime.now(timezone.utc)).state == "unknown"


@pytest.mark.parametrize("field,value", [("raw_csv", "time,value\n0,1"), ("sequence", 0), ("snapshot_age_seconds", float("nan")), ("plc", {"powered_channels": [5]})])
def test_contract_rejects_raw_csv_and_invalid_heartbeats(field, value):
    payload = {"installation_id": str(uuid4()), "station_name": "QA", "sequence": 1, "snapshot_age_seconds": 0}
    payload[field] = value
    with pytest.raises(ValidationError):
        HmiHeartbeat.model_validate(payload)
