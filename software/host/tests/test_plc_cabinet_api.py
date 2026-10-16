from __future__ import annotations

import time
from pathlib import Path

from fastapi.testclient import TestClient

from factory_hmi.gateway.app import create_app

def _post(client, path, payload):
    import uuid
    ctx = client.get("/api/v1/plc/snapshot").json()["coordination"]
    return client.post(path, headers={"X-RP1-Client-ID": "test-window"}, json=dict(
        payload, request_id=str(uuid.uuid4()), expected_epoch=ctx["epoch"], expected_revision=ctx["revision"],
    ))



def test_ps1_setpoint_api_waits_for_mock_plc_confirmation(tmp_path: Path) -> None:
    with TestClient(create_app(data_root=tmp_path)) as client:
        response = _post(client,
            "/api/v1/plc/setpoints",
            {"voltage": 24.0, "current": 8.0, "user": "tester"},
        )
        assert response.status_code == 200
        snapshot = response.json()["result"]

        deadline = time.monotonic() + 2.0
        while (
            snapshot["feedback"]["Setpoints"]["state"] == "pending"
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
            snapshot = client.get("/api/v1/plc/snapshot").json()

        assert snapshot["feedback"]["Setpoints"]["state"] == "confirmed"
        assert snapshot["status"]["PS1SetVoltage"] == 24.0
        assert snapshot["status"]["PS1SetCurrent"] == 8.0


def test_ps1_setpoint_api_rejects_configured_range_violation(tmp_path: Path) -> None:
    with TestClient(create_app(data_root=tmp_path)) as client:
        response = _post(client,
            "/api/v1/plc/setpoints",
            {"voltage": 80.0, "current": 8.0, "user": "tester"},
        )

        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "rejected"
        assert "voltage" in response.json()["detail"]["message"]


def test_start_api_allows_power_sequence_without_selected_channels(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(data_root=tmp_path)) as client:
        response = _post(client,
            "/api/v1/plc/start",
            {
                "channels": [],
                "voltage": 3.0,
                "current": 0.5,
                "user": "tester",
            },
        )
        assert response.status_code == 200
        snapshot = response.json()["result"]

        deadline = time.monotonic() + 2.0
        while snapshot["phase"] != "RUNNING" and time.monotonic() < deadline:
            time.sleep(0.02)
            snapshot = client.get("/api/v1/plc/snapshot").json()

        assert snapshot["phase"] == "RUNNING"
        assert snapshot["status"]["PS1ActualOutput"] is True
        assert snapshot["status"]["ChannelPermit"] == [False] * 4
        assert snapshot["feedback"]["Start"]["state"] == "confirmed"
