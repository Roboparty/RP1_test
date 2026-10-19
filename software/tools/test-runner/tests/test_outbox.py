import json
import sqlite3

import pytest

from rp1_test.api import (
    BY_PRODUCER_ROOT,
    EXECUTIONS_PATH,
    INGESTION_ROOT,
    IngestionClient,
)
from rp1_test.outbox import SQLiteOutbox


def test_idempotency_key_deduplicates(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    first = outbox.enqueue("POST", "/api/v1/ingestion/events", {"n": 1}, idempotency_key="same")
    second = outbox.enqueue("POST", "/api/v1/ingestion/events", {"n": 2}, idempotency_key="same")
    assert first == second == "same"
    pending = outbox.pending()
    assert len(pending) == 1
    assert pending[0].payload == {"n": 1}


def test_existing_outbox_schema_is_migrated_without_losing_items(tmp_path):
    path = tmp_path / "outbox.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            """
            CREATE TABLE outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0,
                last_error TEXT,
                created_at REAL NOT NULL
            )
            """
        )
        db.execute(
            """
            INSERT INTO outbox(
                idempotency_key, method, path, payload_json, created_at
            ) VALUES ('legacy-key', 'POST', '/legacy', '{}', 0)
            """
        )
    outbox = SQLiteOutbox(path)
    pending = outbox.pending()
    assert len(pending) == 1
    assert pending[0].idempotency_key == "legacy-key"
    assert pending[0].producer_execution_key is None
    assert not pending[0].requires_stage_id


def test_flush_marks_success_and_keeps_failure(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue("POST", "/ok", {}, idempotency_key="ok")
    outbox.enqueue("POST", "/fail", {}, idempotency_key="fail")

    def sender(item):
        if item.path == "/fail":
            raise OSError("offline")

    assert outbox.flush(sender) == (1, 1)
    assert outbox.count("sent") == 1
    assert outbox.count("pending") == 1


def test_ingestion_operations_use_real_paths_and_exact_payloads(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    client = IngestionClient("http://platform.invalid", "token", outbox)
    producer_key = "line:e1"
    create = {"producer_key": producer_key, "stage_code": "S"}
    client.create_execution(create, "create-key")
    client.heartbeat(producer_key, {"producer_key": "heartbeat-1"}, "heartbeat-key")
    client.events(producer_key, {"producer_key": "events-1", "events": []}, "events-key")
    client.telemetry(
        producer_key, {"producer_key": "telemetry-1", "series": []}, "telemetry-key"
    )
    client.artifact(
        producer_key, {"producer_key": "artifact-1"}, "artifact-key"
    )
    client.finish_execution(
        producer_key, {"producer_key": "finish-1"}, "finish-key"
    )
    pending = outbox.pending()
    assert len(pending) == 6
    assert all(item.path.startswith(f"{INGESTION_ROOT}/") for item in pending)
    assert pending[0].path == EXECUTIONS_PATH
    assert [item.path for item in pending[1:]] == [
        f"{BY_PRODUCER_ROOT}/{producer_key}/{operation}"
        for operation in ("heartbeat", "events", "telemetry", "artifacts", "finish")
    ]
    assert pending[0].payload == create
    assert "schema_version" not in pending[0].payload
    assert "producer_execution_key" not in pending[0].payload
    assert pending[3].requires_stage_id


def test_producer_path_rejects_slashes_instead_of_quoting(tmp_path):
    client = IngestionClient("", "", SQLiteOutbox(tmp_path / "outbox.sqlite3"))
    with pytest.raises(ValueError, match="must match"):
        client.events("line/e1", {"producer_key": "events-1", "events": []}, "events")


def test_flush_stops_after_first_failure_to_preserve_order(tmp_path):
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    outbox.enqueue("POST", "/first", {}, idempotency_key="first")
    outbox.enqueue("POST", "/second", {}, idempotency_key="second")
    attempted = []

    def sender(item):
        attempted.append(item.path)
        raise OSError("offline")

    assert outbox.flush(sender) == (0, 1)
    assert attempted == ["/first"]


def test_flush_sends_bearer_and_idempotency_headers(tmp_path, monkeypatch):
    outbox = SQLiteOutbox(tmp_path / "outbox.sqlite3")
    client = IngestionClient("https://platform.example", "secret", outbox)
    client.create_execution({"producer_key": "run-1"}, "stable-key")
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(
                {
                    "data": {
                        "execution_id": "11111111-1111-4111-8111-111111111111",
                        "stage_id": "22222222-2222-4222-8222-222222222222",
                        "replayed": True,
                    }
                }
            ).encode()

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert client.flush() == (1, 0)
    request = captured["request"]
    assert request.full_url == f"https://platform.example{EXECUTIONS_PATH}"
    assert request.get_header("Authorization") == "Bearer secret"
    assert request.get_header("Idempotency-key") == "stable-key"
    assert json.loads(request.data) == {"producer_key": "run-1"}


def test_create_resolution_survives_restart_and_fills_telemetry_stage(
    tmp_path, monkeypatch
):
    path = tmp_path / "outbox.sqlite3"
    outbox = SQLiteOutbox(path)
    client = IngestionClient("https://platform.example", "secret", outbox)
    client.create_execution({"producer_key": "run-1"}, "create-key")
    client.telemetry(
        "run-1",
        {"producer_key": "telemetry-1", "series": [{"placeholder": True}]},
        "telemetry-key",
    )
    sent_payloads = []

    class Response:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps(self.body).encode()

    def urlopen(request, timeout):
        del timeout
        payload = json.loads(request.data)
        sent_payloads.append(payload)
        if request.full_url.endswith("/executions"):
            return Response(
                {
                    "data": {
                        "execution_id": "11111111-1111-4111-8111-111111111111",
                        "stage_id": "22222222-2222-4222-8222-222222222222",
                        "replayed": True,
                    }
                }
            )
        return Response({"data": {"status": "ACCEPTED"}})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    assert client.flush(limit=1) == (1, 0)
    reopened = SQLiteOutbox(path)
    assert reopened.get_execution_resolution("run-1") == {
        "execution_id": "11111111-1111-4111-8111-111111111111",
        "stage_id": "22222222-2222-4222-8222-222222222222",
    }
    restarted_client = IngestionClient("https://platform.example", "secret", reopened)
    assert restarted_client.flush() == (1, 0)
    assert sent_payloads[-1]["stage_id"] == "22222222-2222-4222-8222-222222222222"
    assert reopened.count("pending") == 0
    with pytest.raises(ValueError, match="conflicting"):
        reopened.save_execution_resolution(
            "run-1",
            execution_id="33333333-3333-4333-8333-333333333333",
            stage_id="44444444-4444-4444-8444-444444444444",
        )
