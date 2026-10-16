import json
from pathlib import Path

import pytest

from factory_hmi.sync.presence import HmiPresencePublisher, build_presence_snapshot, installation_id
from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
from factory_hmi.sync.uploader import PlatformError


class Client:
    def __init__(self):
        self.messages = []
        self.closed = []

    def heartbeat_presence(self, connection_id, payload):
        self.messages.append((connection_id, payload))
        return {}

    def disconnect_presence(self, connection_id):
        self.closed.append(connection_id)


def test_station_identity_survives_gateway_restart(tmp_path: Path):
    assert installation_id(tmp_path) == installation_id(tmp_path)


def test_snapshot_contains_all_sessions_but_no_raw_csv_paths_or_secrets():
    snapshot = build_presence_snapshot({
        "left": {"state": "running", "playback": {"test_id": "LOCAL_1", "execution_uuid": "11111111-1111-4111-8111-111111111111", "execution_code": "REL-ARM-002_E1", "test_case_id": "REL-ARM-002", "robot_id": "SAMPLE-1", "progress": {"active_seconds": 12.5, "completed_cycles": 3}}, "record_path": "/private/raw.csv", "token": "SECRET"},
        "right": {"state": "paused", "playback": {"test_id": "TEST-2"}},
    }, {"driver": "mock", "phase": "RUNNING", "stale": False, "status": {"ChannelPermit": [True, False, True, False]}})
    assert len(snapshot["runs"]) == 2
    assert snapshot["runs"][0]["active_seconds"] == 12.5
    assert snapshot["runs"][0]["execution_code"] == "REL-ARM-002_E1"
    assert snapshot["runs"][0]["execution_uuid"] == "11111111-1111-4111-8111-111111111111"
    assert snapshot["runs"][0]["case_code"] == "REL-ARM-002"
    assert snapshot["runs"][1]["state"] == "paused"
    assert snapshot["runs"][1]["execution_uuid"] is None
    assert snapshot["plc"]["powered_channels"] == [1, 3]
    assert "raw.csv" not in json.dumps(snapshot)
    assert "SECRET" not in json.dumps(snapshot)


@pytest.mark.parametrize("value,expected", [("not-a-uuid", None), ("11111111-1111-4111-8111-111111111111", "11111111-1111-4111-8111-111111111111")])
def test_snapshot_uuid_falls_back_to_current_platform_context(value, expected):
    snapshot = build_presence_snapshot({"test": {"playback": {"test_id": "local"}, "platform": {"execution_uuid": value}}}, {})
    assert snapshot["runs"][0]["execution_uuid"] == expected


def test_saved_bench_code_is_sent_on_the_heartbeat(tmp_path: Path):
    (tmp_path / "bench-code").write_text("a10\n", encoding="utf-8")
    client = Client()
    publisher = HmiPresencePublisher(client, data_root=tmp_path, snapshot_provider=lambda: {"runs": [], "plc": {}}, interval=60)
    publisher.bench_id = "RD-01"
    publisher.connection_id = "test"
    publisher.send_once()
    assert client.messages[-1][1]["bench_id"] == "a10"


def test_heartbeat_is_independent_of_upload_queue_and_disconnects(tmp_path: Path):
    client = Client()
    publisher = HmiPresencePublisher(client, data_root=tmp_path, snapshot_provider=lambda: {"runs": [], "plc": {}}, interval=60)
    publisher.connect()
    assert len(client.messages) == 1
    first_connection = publisher.connection_id
    assert publisher.status()["online"] is True
    publisher.send_once()
    assert client.messages[-1][1]["sequence"] == 2
    publisher.disconnect()
    assert client.closed == [first_connection]
    assert publisher.status()["online"] is False


def test_failed_snapshot_collection_does_not_claim_fresh_test_state(tmp_path: Path):
    def broken_snapshot():
        raise RuntimeError("controller unavailable")

    client = Client()
    publisher = HmiPresencePublisher(client, data_root=tmp_path, snapshot_provider=broken_snapshot)
    publisher.connection_id = "test"
    publisher.send_once()
    assert client.messages[0][1]["snapshot_age_seconds"] == 86400


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -100])
def test_nonfinite_or_negative_duration_is_not_sent(value):
    snapshot = build_presence_snapshot({"test": {"progress": {"active_seconds": value}}}, {})
    assert snapshot["runs"][0]["active_seconds"] == 0


def test_forced_password_change_revokes_unused_login(tmp_path: Path):
    class PasswordClient:
        logged_out = False

        def authenticate_operator(self, username, password):
            return {"user": {"must_change_password": True}}

        def logout_operator(self):
            self.logged_out = True

    client = PasswordClient()
    worker = ReliabilityUploaderWorker(Outbox(tmp_path / "outbox.sqlite3"), client)
    with pytest.raises(PlatformError, match="首次登录改密"):
        worker.authenticate("operator", "password")
    assert client.logged_out
    assert worker._identity == ""


def test_cleanup_failure_preserves_original_login_error(tmp_path: Path):
    class LoginClient:
        def authenticate_operator(self, username, password):
            return {"user": {"username": username}}

        def logout_operator(self):
            raise PlatformError("network unavailable during cleanup")

    class RejectedPresence:
        def connect(self):
            raise PlatformError("account cannot publish status", status=403)

        def disconnect(self):
            pass

    worker = ReliabilityUploaderWorker(Outbox(tmp_path / "outbox.sqlite3"), LoginClient(), presence=RejectedPresence())
    with pytest.raises(PlatformError, match="account cannot publish status"):
        worker.authenticate("viewer", "password")
    assert worker._identity == ""
