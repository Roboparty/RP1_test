"""Real HMI serialization over TCP, cookies/CSRF, processing and self approval.

All source samples, accounts and review reasons are synthetic. The dedicated
runner supplies a disposable database/storage and mounts the HMI source at /hmi.
Only receipt loss is injected: every affected HTTP request first reaches and
commits on the actual FastAPI server.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import tarfile
import threading
import time
from uuid import uuid4

import pytest
from sqlalchemy import text
import uvicorn

from app.core.config import get_settings
from app.main import app
from app.services.archive_storage import ArchiveStorage
from app.services.archive_worker import run_once
from test_hmi_presence_integration import PASSWORD, environment, signed_in
from test_submissions_integration import context, grant


HMI_AVAILABLE = importlib.util.find_spec("factory_hmi") is not None
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="requires isolated archive database"),
    pytest.mark.skipif(not HMI_AVAILABLE, reason="requires mounted HMI source on PYTHONPATH"),
]
CONTEXT_FIELDS = ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")


class CaptureWire:
    """Observe request routing/authentication presence without retaining secrets."""
    def __init__(self, application):
        self.application = application
        self.requests = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            self.requests.append({
                "method": scope["method"], "path": scope["path"],
                "query": scope["query_string"].decode("ascii"),
                "content_type": headers.get(b"content-type", b"").decode("ascii"),
                "has_session": b"rp1_session=" in headers.get(b"cookie", b""),
                "has_csrf": bool(headers.get(b"x-csrf-token")),
                "has_bearer": b"authorization" in headers,
            })
        await self.application(scope, receive, send)


@pytest.fixture
def real_server(environment):
    # No exposed host port: this listener is inside the disposable QA container.
    captured = CaptureWire(app)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(captured, log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started, "isolated TCP server did not start"
    try:
        yield base_url, captured, environment
    finally:
        server.should_exit = True
        thread.join(5)
        listener.close()
        assert not thread.is_alive(), "isolated TCP server did not stop"


def hmi_client(base_url, lost_receipt=None):
    from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient
    from factory_hmi.sync.uploader import PlatformError

    class ReceiptLossClient(ReliabilityPlatformClient):
        def __init__(self):
            super().__init__(base_url)
            self.lost = False
            self.calls = []

        def _call(self, method, path, **kwargs):
            result = super()._call(method, path, **kwargs)
            self.calls.append((method, path, deepcopy(kwargs.get("payload")), kwargs.get("idempotency_key")))
            operation = "create" if method == "POST" and path == "/api/v1/archives" else (
                "chunk" if method == "PUT" and "/content?offset=" in path else path.rsplit("/", 1)[-1])
            if lost_receipt == operation and not self.lost:
                self.lost = True
                raise PlatformError("Synthetic receipt lost after the real HTTP response committed")
            return result

    return ReceiptLossClient()


def classified_profile(engine, users, worker):
    with signed_in(users["admin"]) as administrator:
        ctx = context(administrator)
        grant(administrator, engine, users["first"], ctx["campaign_id"])
    return next(profile for profile in worker.context_profiles()
                if profile["campaign_id"] == ctx["campaign_id"] and profile["station_id"] == ctx["station_id"])


def local_record(tmp_path, profile, *, origin="HMI", old_passed_summary=False):
    from factory_hmi.core.service import FactoryService
    import factory_hmi
    import yaml

    service = FactoryService(data_root=tmp_path / "local-hmi")
    source = tmp_path / "synthetic.csv"
    source.write_text("time_s,hip_cmd_pos_rad,hip_pos_rad,hip_torque_nm,hip_temp_c,hip_error_id\n" +
                      "".join(f"{index/10:.1f},{index/100:.2f},{index/100:.2f},1,25,0\n" for index in range(21)), encoding="utf-8")
    configuration = yaml.safe_load((Path(factory_hmi.__file__).parent / "config/station_defaults.yaml").read_text(encoding="utf-8"))
    end = datetime.now(timezone.utc) - timedelta(minutes=10)
    test_id = "synthetic-wire-" + uuid4().hex
    initial = {
        **{key: profile[key] for key in CONTEXT_FIELDS},
        "execution_uuid": str(uuid4()), "test_id": test_id,
        "test_case_id": profile["test_case_id"] if origin == "HMI" else "OTHER",
        "sample_id": profile["sample_id"], "bench_id": profile["bench_id"],
        "recording_scope": "platform" if origin == "HMI" else "local",
        "test_name": "Synthetic full wire test", "actual_object": "Synthetic physical object",
        "started_at": (end - timedelta(seconds=2)).isoformat(), "ended_at": end.isoformat(),
        "active_seconds": 2, "completed_cycles": 1, "record_rate_hz": 10,
        "outcome": "completed", "origin": origin, "config": configuration,
    }
    report = service.report_manager.finalize(source, initial)
    assert report.summary["verdict"]["criteria_version"] == "generic-v1"
    assert report.summary["verdict"]["status"] == "unconfigured", "default missing rated torque must not automatically pass"
    if old_passed_summary:
        # A pre-upgrade local report could say passed while current central rules
        # say unconfigured. Preserve all raw bytes and model only that old report.
        legacy = deepcopy(report.summary)
        legacy["verdict"]["status"] = "passed"
        report.summary_path.write_text(json.dumps(legacy), encoding="utf-8")
        with tarfile.open(report.bundle_path, "w:gz") as bundle:
            for path in sorted(report.execution_dir.rglob("*")):
                if path.is_file():
                    bundle.add(path, arcname=path.relative_to(report.execution_dir).as_posix())
    review = {"tester": "Synthetic actual tester", "bench": profile["bench_id"], "station_id": profile["station_id"],
              "outcome": "成功", "notes": "Synthetic operator notes", "test_name": initial["test_name"],
              "actual_object": initial["actual_object"],
              "discrepancy_reason": "" if old_passed_summary else "Synthetic operator explicitly accepts engineering completion; rated torque criteria are not configured"}
    return service, report, review


def submit_locally(service, report, review, owner):
    row = service.submit_execution(report.manifest["test_id"], review=review)
    service.outbox.update(row["execution_uuid"], submission_owner=owner)
    return row["execution_uuid"]


def drive(worker, service, execution_uuid, expected):
    for _ in range(15):
        item = service.outbox.get(execution_uuid)
        if item.state == expected:
            return item
        service.outbox.update(execution_uuid, next_attempt_at=0)
        worker.process_once()
        run_once()
    item = service.outbox.get(execution_uuid)
    assert item.state == expected, f"expected {expected}; got {item.state}: {item.last_error}"
    return item


def assert_csv_wire(captured, execution_uuid, raw, detail):
    uploads = [call for call in captured.requests if call["method"] == "PUT"]
    assert uploads and all(call["path"] == f"/api/v1/archives/{detail['id']}/content" for call in uploads)
    assert all(call["content_type"] == "application/octet-stream" and call["has_csrf"] and call["has_session"] for call in uploads)
    assert not any(call["has_bearer"] or call["path"].startswith(("/api/v1/ingestion", "/api/uploads", "/api/submissions")) for call in captured.requests)
    assert detail["received_bytes"] == len(raw)
    assert detail["sha256"] == hashlib.sha256(raw).hexdigest()
    assert ArchiveStorage(get_settings().archive_root).path(execution_uuid, "raw.csv").read_bytes() == raw


def test_real_hmi_generic_defaults_cookie_csrf_processing_self_approval_and_curves(real_server, tmp_path):
    from factory_hmi.core.identity import build_execution_code
    from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
    from factory_hmi.sync.outbox import Outbox
    from factory_hmi.sync.uploader import PlatformError

    base_url, captured, (engine, users) = real_server
    client = hmi_client(base_url)
    worker = ReliabilityUploaderWorker(Outbox(tmp_path / "bootstrap.sqlite3"), client)
    worker.authenticate(users["first"], PASSWORD)
    profile = classified_profile(engine, users, worker)
    service, report, review = local_record(tmp_path, profile)
    worker.outbox = service.outbox
    execution_uuid = submit_locally(service, report, review, users["first"])
    pending = drive(worker, service, execution_uuid, "pending_review")
    detail = client.archive(pending.upload_id)
    assert detail["processing_state"] == "READY" and detail["submission_status"] == "PENDING"
    assert detail["execution_uuid"] == execution_uuid and detail["owner_name"] == users["first"]
    assert detail["is_owner"] is True and detail["station_label"] == profile["bench_id"]
    assert detail["facts"]["automatic_verdict"] == "unconfigured"
    assert detail["current_verification"]["tester"] == review["tester"]
    assert detail["current_verification"]["notes"] == review["notes"]
    receipt = client.submission(pending.submission_id)
    assert receipt["submitter"] == users["first"] and receipt["execution_uuid"] == execution_uuid
    with engine.connect() as database:
        assert database.execute(text("SELECT count(*) FROM test.test_execution WHERE approved_submission_id=(SELECT id FROM integration.data_submission WHERE public_id=:id)"), {"id": pending.submission_id}).scalar_one() == 0
    # Default urllib CookieJar must supply both session and CSRF on actual TCP.
    csrf_cookie = next(cookie for cookie in client.cookies if cookie.name == "rp1_csrf")
    client.cookies.clear(csrf_cookie.domain, csrf_cookie.path, csrf_cookie.name)
    decision = {"revision": pending.submission_revision, "decision": "APPROVED", "reason": "Synthetic self review"}
    with pytest.raises(PlatformError) as rejected:
        client._call("POST", f"/api/v1/submissions/{pending.submission_id}/review", payload=decision, idempotency_key=uuid4().hex, service_auth=False)
    assert rejected.value.status == 403
    worker.authenticate(users["first"], PASSWORD)
    approved = client._call("POST", f"/api/v1/submissions/{pending.submission_id}/review", payload=decision, idempotency_key=uuid4().hex, service_auth=False)
    assert approved["status"] == "APPROVED" and approved["execution_id"]
    local = drive(worker, service, execution_uuid, "approved")
    expected_code = build_execution_code(profile["test_case_id"], datetime.fromisoformat(report.manifest["started_at"].replace("Z", "+00:00")), profile["sample_id"], profile["bench_id"])
    assert local.execution_code == expected_code or local.execution_code.startswith(expected_code + "_E")
    curves = client._call("GET", f"/api/v1/archives/{approved['execution_id']}/curves?metrics=pos_rad&max_points=50", service_auth=False)
    assert curves["series"][0]["points"] == [[index/10, index/100] for index in range(21)]
    assert_csv_wire(captured, execution_uuid, (report.execution_dir / "telemetry/raw.csv").read_bytes(), client.archive(pending.upload_id))


def test_real_hmi_other_archive_stops_before_formal_submission(real_server, tmp_path):
    from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
    from factory_hmi.sync.outbox import Outbox

    base_url, captured, (engine, users) = real_server
    client = hmi_client(base_url)
    worker = ReliabilityUploaderWorker(Outbox(tmp_path / "bootstrap.sqlite3"), client)
    worker.authenticate(users["first"], PASSWORD)
    profile = classified_profile(engine, users, worker)
    service, report, review = local_record(tmp_path, profile, origin="OTHER")
    worker.outbox = service.outbox
    execution_uuid = submit_locally(service, report, review, users["first"])
    local = drive(worker, service, execution_uuid, "awaiting_classification")
    detail = client.archive(local.upload_id)
    assert detail["origin"] == "OTHER" and detail["classification_state"] == "UNCLASSIFIED"
    assert detail["test_name"] == review["test_name"] and detail["actual_object"] == review["actual_object"]
    assert detail["submission_id"] is None and not local.submission_id
    assert not any(call[1].endswith("/submit") for call in client.calls)
    assert client._call("GET", f"/api/v1/archives/{local.upload_id}/curves?metrics=pos_rad", service_auth=False)["series"]
    assert_csv_wire(captured, execution_uuid, (report.execution_dir / "telemetry/raw.csv").read_bytes(), detail)


@pytest.mark.parametrize("lost_receipt", ["create", "chunk", "verification", "submit"])
def test_real_server_committed_receipt_loss_resumes_without_duplicate_evidence(real_server, tmp_path, lost_receipt):
    from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
    from factory_hmi.sync.outbox import Outbox

    base_url, captured, (engine, users) = real_server
    client = hmi_client(base_url, lost_receipt)
    worker = ReliabilityUploaderWorker(Outbox(tmp_path / "bootstrap.sqlite3"), client)
    worker.authenticate(users["first"], PASSWORD)
    profile = classified_profile(engine, users, worker)
    service, report, review = local_record(tmp_path, profile)
    worker.outbox = service.outbox
    execution_uuid = submit_locally(service, report, review, users["first"])
    local = drive(worker, service, execution_uuid, "pending_review")
    assert client.lost
    detail = client.archive(local.upload_id)
    assert len(detail["verification_history"]) == 1
    assert len(client.submission(local.submission_id)["revisions"]) == 1
    if lost_receipt == "chunk":
        assert len([call for call in captured.requests if call["method"] == "PUT"]) == 1
    if lost_receipt in {"verification", "submit", "create"}:
        repeated = [call for call in client.calls if (call[1] == "/api/v1/archives" if lost_receipt == "create" else call[1].endswith("/" + lost_receipt))]
        assert len(repeated) == 2 and repeated[0][2:] == repeated[1][2:]
    with engine.connect() as database:
        assert database.execute(text("SELECT count(*) FROM integration.execution_archive WHERE execution_uuid=:id"), {"id": execution_uuid}).scalar_one() == 1
        assert database.execute(text("SELECT count(*) FROM integration.data_submission WHERE execution_uuid=:id"), {"id": execution_uuid}).scalar_one() == 1
    assert_csv_wire(captured, execution_uuid, (report.execution_dir / "telemetry/raw.csv").read_bytes(), detail)


def test_central_difference_requires_actual_manual_reason_and_preserves_original_create(real_server, tmp_path):
    from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
    from factory_hmi.sync.outbox import Outbox

    base_url, captured, (engine, users) = real_server
    client = hmi_client(base_url)
    worker = ReliabilityUploaderWorker(Outbox(tmp_path / "bootstrap.sqlite3"), client)
    worker.authenticate(users["first"], PASSWORD)
    profile = classified_profile(engine, users, worker)
    service, report, review = local_record(tmp_path, profile, old_passed_summary=True)
    worker.outbox = service.outbox
    execution_uuid = submit_locally(service, report, review, users["first"])
    frozen_review = drive(worker, service, execution_uuid, "needs_verification")
    frozen_create = json.loads(frozen_review.submission_payload)["create"]
    assert frozen_create["facts"]["automatic_verdict"] == "passed"
    actual_analysis = service.execution_summary(report.manifest["test_id"])["central_analysis"]
    assert actual_analysis["verdict"] == "unconfigured"
    assert client.archive(frozen_review.upload_id)["verification_history"] == []
    with pytest.raises(ValueError, match="差异原因"):
        service.submit_execution(report.manifest["test_id"], review=review)
    with pytest.raises(ValueError):
        service.retry_submission(report.manifest["test_id"])
    # The reason is explicitly authored here, never generated from the verdict.
    correction = {**review, "notes": "Synthetic reviewed central result",
                  "discrepancy_reason": "Synthetic tester reviewed missing rated torque criteria and explicitly retains the engineering completion verdict"}
    submit_locally(service, report, correction, users["first"])
    pending = drive(worker, service, execution_uuid, "pending_review")
    assert json.loads(pending.submission_payload)["create"] == frozen_create
    detail = client.archive(pending.upload_id)
    assert detail["current_verification"]["discrepancy_reason"] == correction["discrepancy_reason"]
    assert detail["facts"]["automatic_verdict"] == "passed"
    assert len(detail["verification_history"]) == 1
    assert_csv_wire(captured, execution_uuid, (report.execution_dir / "telemetry/raw.csv").read_bytes(), detail)
