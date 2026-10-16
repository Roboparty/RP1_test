from copy import deepcopy
import json
from pathlib import Path
import pytest

from factory_hmi.core.report import ReportManager
from factory_hmi.sync.outbox import Outbox
from factory_hmi.sync.uploader import PlatformError
from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient, ReliabilityUploaderWorker, load_test_profiles

def test_profile_loader_marks_unfrozen_database_context(tmp_path: Path) -> None:
    path = tmp_path / "profiles.yaml"
    path.write_text(
        """
profiles:
  - key: pending
    label: 待冻结
    campaign_id: null
    stage_code: AGING
""",
        encoding="utf-8",
    )
    profiles = load_test_profiles(path)
    assert profiles[0]["ready"] is False
    assert "campaign_id" in profiles[0]["missing"]
    assert "station_id" in profiles[0]["missing"]


class _FakeReliabilityClient:
    def __init__(self):
        self.calls = []
        self.receipt = None
        self.username = "offline"
        self.interrupt = False

    def authenticate_operator(self, username, password):
        self.username = username
        return {"user": {"username": username}}

    def submit_summary(self, payload, key):
        self.calls.append(("submit", deepcopy(payload), key))
        if self.receipt is None or self.receipt["status"] == "REJECTED":
            self.receipt = {"id": "00000000-0000-7000-8000-000000000011",
                "execution_uuid": payload["summary"]["execution_uuid"], "submitter": self.username,
                "revision": payload["expected_revision"] + 1, "status": "PENDING",
                "execution_code": None, "revisions": [{"review_reason": None}]}
        if self.interrupt:
            self.interrupt = False
            raise PlatformError("simulated lost response")
        return deepcopy(self.receipt)

    def submission(self, submission_id):
        self.calls.append(("poll", submission_id))
        return deepcopy(self.receipt)

def _offline_upload(root, client):
    csv_path = root / "offline.csv"
    csv_path.write_text("time_s,hip_pos_rad\n0,0.1\n1,0.2\n")
    context = {
        "execution_uuid": "11111111-1111-4111-8111-111111111111",
        "test_id": "offline-record", "test_case_id": "REL-UPPER-001",
        "sample_id": "RP1.3-UPPER-001", "bench_id": "MP-01",
        "started_at": "2026-09-22T00:00:00Z", "ended_at": "2026-09-22T00:00:01Z",
        "outcome": "completed", "record_rate_hz": 1,
        "subject_map": {"hip": "UPPER-HIP"},
        **{key: "00000000-0000-7000-8000-000000000001" for key in (
            "campaign_id", "cycle_id", "segment_id", "asset_id",
            "configuration_id", "test_case_version_id", "station_id",
        )},
    }
    report = ReportManager(root / "data").finalize(csv_path, context)
    outbox = Outbox(root / "outbox.sqlite3")
    outbox.enqueue(report.manifest, report.bundle_path)
    outbox.update(report.manifest["execution_uuid"], submission_owner="offline")
    worker = ReliabilityUploaderWorker(outbox, client)
    worker.authenticate("offline", "placeholder")
    return worker, outbox, report


def test_submission_transmits_only_bounded_summary_and_waits_for_review(tmp_path):
    client = _FakeReliabilityClient()
    worker, outbox, report = _offline_upload(tmp_path, client)
    assert worker.process_once() == 1
    item = outbox.get(report.manifest["execution_uuid"])
    assert item.state == "pending_review" and not item.execution_code
    payload = client.calls[0][1]
    assert payload["expected_revision"] == 0
    assert set(payload["summary"]) == {"execution_uuid", "campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id", "started_at", "ended_at", "active_seconds", "completed_cycles", "source_status", "data_quality", "clock_quality", "exception_count", "summary", "evidence_sha256"}
    wire = json.dumps(payload)
    assert len(wire) < 8192
    assert all(word not in wire for word in ("raw.csv", "points", "bundle_path", "subject_map", "hip_pos_rad", "base64"))
    assert report.bundle_path.is_file()
    outbox.update(item.execution_uuid, next_attempt_at=0)
    worker.process_once()
    assert [c[0] for c in client.calls] == ["submit", "poll"]
    client.receipt.update(status="APPROVED", execution_code="REL-UPPER-001_2609220800_RP1.3-UPPER-001_MP-01_E11")
    outbox.update(item.execution_uuid, next_attempt_at=0)
    worker.process_once()
    assert outbox.get(item.execution_uuid).state == "approved"
    assert outbox.retry(item.execution_uuid).state == "approved"
    assert worker.process_once() == 0


def test_rejection_is_terminal_until_explicit_resubmission_and_uses_new_revision(tmp_path):
    client = _FakeReliabilityClient()
    worker, outbox, report = _offline_upload(tmp_path, client)
    worker.process_once()
    client.receipt.update(status="REJECTED", revisions=[{"review_reason": "please correct summary"}])
    key = report.manifest["execution_uuid"]
    outbox.update(key, next_attempt_at=0)
    worker.process_once()
    assert outbox.get(key).state == "rejected"
    assert worker.process_once() == 0
    outbox.retry(key)
    worker.process_once()
    submitted = [c for c in client.calls if c[0] == "submit"]
    assert submitted[-1][1]["expected_revision"] == 1
    assert submitted[0][2] != submitted[-1][2]
    assert outbox.get(key).submission_revision == 2


def test_lost_response_reuses_request_key_and_restart_keeps_owner(tmp_path):
    client = _FakeReliabilityClient()
    worker, outbox, report = _offline_upload(tmp_path, client)
    client.interrupt = True
    worker.process_once()
    key = report.manifest["execution_uuid"]
    assert outbox.get(key).state == "sync_failed"
    # Rebuilding the local bundle after a lost response must not alter the request.
    report.bundle_path.write_bytes(b"changed local bundle after first request")
    outbox.retry(key)
    worker.process_once()
    requests = [c for c in client.calls if c[0] == "submit"]
    assert requests[0][1:] == requests[1][1:]
    restored = Outbox(outbox.path).get(key)
    assert restored.submission_owner == "offline" and restored.submission_revision == 1


def test_another_account_or_unclaimed_legacy_queue_is_never_auto_submitted(tmp_path):
    client = _FakeReliabilityClient()
    worker, outbox, report = _offline_upload(tmp_path, client)
    key = report.manifest["execution_uuid"]
    outbox.update(key, submission_owner="other")
    assert worker.process_once() == 0 and not client.calls
    outbox.update(key, submission_owner="")
    assert worker.process_once() == 0 and not client.calls


def test_foreign_queues_do_not_starve_the_signed_in_submitter(tmp_path):
    from uuid import uuid4
    client = _FakeReliabilityClient()
    worker, outbox, report = _offline_upload(tmp_path, client)
    for index in range(15):
        manifest = {**report.manifest, "execution_uuid": str(uuid4()), "test_id": f"foreign-{index}"}
        row = outbox.enqueue(manifest, report.bundle_path)
        outbox.update(row.execution_uuid, submission_owner="other")
    outbox.update(report.manifest["execution_uuid"], attempts=1)
    assert worker.process_once() == 1
    assert len(client.calls) == 1


@pytest.mark.parametrize("bad_code", ["", "REL-SARM-001_2609220800_RP1.3-SARM-001_MP-01_E11"])
def test_wrong_approved_execution_identity_is_not_accepted(tmp_path, bad_code):
    client = _FakeReliabilityClient()
    worker, outbox, report = _offline_upload(tmp_path, client)
    worker.process_once()
    client.receipt.update(status="APPROVED", execution_code=bad_code)
    key = report.manifest["execution_uuid"]
    outbox.update(key, next_attempt_at=0)
    worker.process_once()
    assert outbox.get(key).state == "sync_failed"
    assert not outbox.get(key).execution_code


def test_legacy_ingestion_client_refuses_telemetry_before_network():
    client = ReliabilityPlatformClient("http://127.0.0.1:1")
    with pytest.raises(PlatformError, match="已停用"):
        client.telemetry("old-run", {}, "synthetic-key")


def test_legacy_binary_transport_is_blocked_before_network():
    from factory_hmi.sync.uploader import PlatformClient
    client = PlatformClient("http://127.0.0.1:1")
    with pytest.raises(PlatformError, match="已停用"):
        client.put_chunk("old-upload", 0, b"synthetic forbidden bundle")
