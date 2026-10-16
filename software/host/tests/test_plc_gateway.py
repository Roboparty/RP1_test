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



def _wait_phase(client: TestClient, expected: str) -> dict:
    deadline = time.monotonic() + 2.0
    snapshot: dict = {}
    while time.monotonic() < deadline:
        response = client.get("/api/v1/plc/snapshot")
        assert response.status_code == 200
        snapshot = response.json()
        if snapshot["phase"] == expected:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"PLC did not reach {expected}: {snapshot}")


def test_gateway_exposes_protocol_neutral_plc_sequence(tmp_path: Path) -> None:
    with TestClient(create_app(data_root=tmp_path)) as client:
        snapshot = client.get("/api/v1/plc/snapshot").json()
        assert snapshot["driver"] == "mock"
        assert snapshot["dut_can_map"] == {
            "DUT1": "can0",
            "DUT2": "can1",
            "DUT3": "can2",
            "DUT4": "can3",
        }

        response = _post(client,
            "/api/v1/plc/start",
            {
                "user": "gateway-test",
                "channels": [1, 2, 3, 4],
                "voltage": 3.0,
                "current": 0.5,
            },
        )
        assert response.status_code == 200
        running = _wait_phase(client, "RUNNING")
        assert running["status"]["PS1ActualOutput"] is True
        assert running["status"]["ChannelPermit"] == [True] * 4
        assert running["status"]["PS1SetVoltage"] == 3.0
        assert running["status"]["PS1SetCurrent"] == 0.5

        response = _post(client,
            "/api/v1/plc/stop",
            {"user": "gateway-test"},
        )
        assert response.status_code == 200
        stopped = _wait_phase(client, "OFF")
        # Power-off invalidates PS1's prior-cycle feedback. TCP still works,
        # but the unavailable device status is never presented as actual OFF.
        assert stopped["status"]["PS1ActualOutput"] is None
        assert stopped["status"]["PS1FeedbackValid"] is False
        assert stopped["status"]["MainContactorFB"] is False
        assert any(
            item["user"] == "gateway-test" and item["confirmation"] == "confirmed"
            for item in stopped["operation_log"]
        )
