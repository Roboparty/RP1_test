"""Verified reliability_v1 transport using synthetic CSV and mock clients only."""
from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from factory_hmi.core.service import FactoryService
from factory_hmi.desktop.operator_review import review_package
from factory_hmi.gateway.schemas import ExecutionActionRequest
from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
from factory_hmi.sync.uploader import PlatformError

ID = "11111111-1111-4111-8111-111111111111"
ARCHIVE = "22222222-2222-4222-8222-222222222222"
SUBMISSION = "33333333-3333-4333-8333-333333333333"
CTX = {key: ID for key in ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")}


@pytest.fixture
def local_record(tmp_path):
    service = FactoryService(data_root=tmp_path / "data")
    csv = tmp_path / "synthetic.csv"
    csv.write_text("time_s,hip_pos_rad,hip_cmd_pos_rad,hip_torque_nm,hip_temp_c\n0,0,0,1,25\n1,0.1,0.2,2,26\n", encoding="utf-8")
    report = service.report_manager.finalize(csv, {"execution_uuid": ID, "test_id": "synthetic-test", "test_case_id": "OTHER",
        "test_name": "临时负载试验", "actual_object": "合成样机", "sample_id": "合成样机", "bench_id": "A10", "recording_scope": "local",
        "started_at": "2026-10-15T00:00:00Z", "ended_at": "2026-10-15T00:00:01Z", "active_seconds": 1,
        "completed_cycles": 1, "outcome": "interrupted", "record_rate_hz": 1})
    review = {"tester": "实际测试人", "bench": "A10", "station_id": ID, "outcome": "中止", "notes": "无",
        "discrepancy_reason": "临时探索测试，尚未配置正式判据", "test_name": "临时负载试验", "actual_object": "合成样机"}
    return service, report, review


class ArchiveClient:
    def __init__(self):
        self.calls = []
        self.detail = None
        self.raw = bytearray()
        self.lose_chunk = False
        self.lose_verify = False
        self.lose_classification = False
        self.cached = {}
        self.receipt = {"id": SUBMISSION, "execution_uuid": ID, "submitter": "submit-account", "status": "PENDING", "revision": 1, "revisions": [{}]}

    def authenticate_operator(self, username, _password):
        return {"user": {"username": username}}

    def create_archive(self, payload, key):
        self.calls.append(("create", deepcopy(payload), key))
        self.detail = {"id": ARCHIVE, "execution_uuid": payload["execution_uuid"], "revision": 0, "received_bytes": 0,
            "processing_state": "WAITING", "classification_state": "CLASSIFIED" if payload.get("classification") else "UNCLASSIFIED",
            "classification": deepcopy(payload.get("classification"))}
        return deepcopy(self.detail)

    def archive(self, archive_id):
        assert archive_id == ARCHIVE
        return deepcopy(self.detail)

    def archive_chunk(self, archive_id, offset, body, key):
        assert archive_id == ARCHIVE and offset == len(self.raw)
        self.calls.append(("chunk", offset, body, key))
        self.raw.extend(body)
        self.detail["received_bytes"] = len(self.raw)
        if self.lose_chunk:
            self.lose_chunk = False
            raise PlatformError("lost chunk response")
        return {"received_bytes": len(self.raw)}

    def archive_action(self, archive_id, action, payload, key):
        assert archive_id == ARCHIVE
        self.calls.append((action, deepcopy(payload), key))
        if key in self.cached:
            assert self.cached[key][0] == payload
            return deepcopy(self.cached[key][1])
        if action == "complete":
            self.detail["processing_state"] = "READY"
        elif action == "verification":
            assert payload["expected_revision"] == self.detail["revision"]
            self.detail["revision"] += 1
        elif action == "classification":
            assert payload["expected_revision"] == self.detail["revision"]
            self.detail["classification"] = deepcopy(payload["context"])
            self.detail["revision"] += 1
        elif action == "submit":
            self.detail.update(submission_id=SUBMISSION, submission_revision=1, submission_status="PENDING")
        result = deepcopy(self.detail)
        self.cached[key] = (deepcopy(payload), result)
        if action == "verification" and self.lose_verify:
            self.lose_verify = False
            raise PlatformError("lost verification response")
        if action == "classification" and self.lose_classification:
            self.lose_classification = False
            raise PlatformError("lost classification response")
        return result

    def submission(self, _id):
        return deepcopy(self.receipt)


def worker_for(service, review, client):
    row = service.submit_execution("synthetic-test", review=review)
    service.outbox.update(row["execution_uuid"], submission_owner="submit-account")
    worker = ReliabilityUploaderWorker(service.outbox, client)
    worker.authenticate("submit-account", "synthetic-placeholder")
    return worker


def test_review_is_required_notes_and_discrepancy_are_required_no_default_pass(local_record):
    service, report, review = local_record
    with pytest.raises(ValueError, match="必须核实"):
        service.submit_execution(report.manifest["test_id"])
    for changes, message in [({"notes": ""}, "记录必填"), ({"outcome": ""}, "请选择"), ({"station_id": "A10"}, "有效工位"),
        ({"discrepancy_reason": ""}, "差异原因"), ({"active_seconds": 999}, "禁止修改")]:
        with pytest.raises(ValueError, match=message):
            service.submit_execution(report.manifest["test_id"], review={**review, **changes})
    assert service.outbox.list() == []


def test_other_archive_sends_only_csv_and_separate_human_verification(local_record):
    service, report, review = local_record
    client = ArchiveClient()
    worker = worker_for(service, review, client)
    assert worker.process_once() == 1
    row = service.outbox.get(ID)
    assert row.state == "awaiting_classification"
    create = next(call[1] for call in client.calls if call[0] == "create")
    assert create["facts"]["machine_status"] == "interrupted"
    assert create["facts"]["automatic_verdict"] == "unconfigured"
    assert create["facts"]["data_quality"] == "PARTIAL"
    assert "classification" not in create
    verification = next(call[1] for call in client.calls if call[0] == "verification")
    assert verification["tester"] == "实际测试人" and verification["verdict"] == "blocked"
    assert "analysis" not in verification and "active_seconds" not in verification
    assert bytes(client.raw) == (report.execution_dir / "telemetry/raw.csv").read_bytes()
    assert not any(call[0] == "submit" for call in client.calls)


def test_chunk_lost_response_resumes_server_offset_and_keeps_identity(local_record):
    service, report, review = local_record
    client = ArchiveClient()
    worker = worker_for(service, review, client)
    client.lose_chunk = True
    worker.process_once()
    assert service.outbox.get(ID).state == "sync_failed"
    service.outbox.retry(ID)
    worker.process_once()
    assert service.outbox.get(ID).state == "awaiting_classification"
    assert len([call for call in client.calls if call[0] == "chunk"]) == 1
    assert len([call for call in client.calls if call[0] == "create"]) == 1


def test_verification_lost_response_reuses_frozen_expected_revision(local_record):
    service, _report, review = local_record
    client = ArchiveClient()
    worker = worker_for(service, review, client)
    client.lose_verify = True
    worker.process_once()
    service.outbox.retry(ID)
    worker.process_once()
    calls = [call for call in client.calls if call[0] == "verification"]
    assert len(calls) == 2 and calls[0][1:] == calls[1][1:]


def test_tampered_csv_is_rejected_before_queue_and_after_lost_response(local_record):
    service, report, review = local_record
    raw = report.execution_dir / "telemetry/raw.csv"
    original = raw.read_bytes()
    raw.write_bytes(original + b"2,0,0,0,0\n")
    with pytest.raises(ValueError, match="证据不一致"):
        service.submit_execution(report.manifest["test_id"], review=review)
    raw.write_bytes(original)
    client = ArchiveClient()
    worker = worker_for(service, review, client)
    client.lose_chunk = True
    worker.process_once()
    raw.write_bytes(original + b"2,0,0,0,0\n")
    service.outbox.retry(ID)
    worker.process_once()
    assert "冻结证据不一致" in service.outbox.get(ID).last_error


@pytest.mark.parametrize("same_size", [True, False])
def test_csv_changed_after_enqueue_is_rejected_before_first_central_request(local_record, same_size):
    service, report, review = local_record
    client = ArchiveClient()
    worker = worker_for(service, review, client)
    raw = report.execution_dir / "telemetry/raw.csv"
    original = raw.read_bytes()
    raw.write_bytes(original.replace(b"25", b"95", 1) if same_size else original + b"2,0,0,0,0\n")
    assert not service.outbox.get(ID).submission_payload
    worker.process_once()
    row = service.outbox.get(ID)
    assert row.state == "sync_failed"
    assert "冻结证据不一致" in row.last_error
    assert not row.submission_payload and not row.upload_id
    assert client.calls == [] and client.raw == bytearray()


def test_review_versions_and_history_import_never_infer_absolute_clock(local_record, tmp_path):
    service, report, review = local_record
    service.report_manager.write_operator_review(report.manifest["test_id"], review)
    service.report_manager.write_operator_review(report.manifest["test_id"], {**review, "notes": "补充记录"})
    service.report_manager.rebuild(report.manifest["test_id"])
    assert len(list((report.execution_dir / "analysis").glob("operator_review.v*.json"))) == 2
    history = tmp_path / "20200101_someone_station2.csv"
    history.write_text("time_s,hip_pos_rad\n0,1\n2,2\n", encoding="utf-8")
    imported = service.import_history([str(history)], {"test_name": "历史补录", "actual_object": "明确对象", "bench_id": "A10", "operator_id": "明确测试人"})
    imported_manifest = service.report_manager.manifest(imported[0]["test_id"])
    assert imported_manifest["timestamp_provenance"] == "missing"
    assert imported_manifest["origin"] == "HISTORY"
    assert imported_manifest["operator_id"] == "明确测试人"
    assert "1970-01-01" not in service.report_manager.report_path(imported[0]["test_id"]).read_text(encoding="utf-8")
    assert next(item for item in service.execution_history() if item["test_id"] == imported[0]["test_id"])["started_at"] == ""
    assert service.outbox.list() == []
    assert service.import_history([str(history)], {"test_name": "x", "actual_object": "x", "bench_id": "x", "operator_id": "x"})[0]["duplicate"]


def test_gateway_rejects_measured_field_edits():
    with pytest.raises(ValueError):
        ExecutionActionRequest(test_id="x", active_seconds=999)


def test_formal_archive_waits_for_approval_and_poll_publishes_receipt(local_record):
    service, report, review = local_record
    client = ArchiveClient()
    worker = worker_for(service, review, client)
    row = service.outbox.get(ID)
    service.outbox.enqueue({**row.manifest, **CTX, "recording_scope": "platform"}, report.bundle_path)
    worker.process_once()
    assert service.outbox.get(ID).state == "pending_review"
    submit = next(call for call in client.calls if call[0] == "submit")
    assert submit[1]["expected_submission_revision"] == 0
    client.receipt.update(status="APPROVED", execution_code="REL-ARM-001_2610150800_RP1.3-ARM-001_A10_E1")
    service.outbox.update(ID, next_attempt_at=0)
    worker.process_once()
    assert service.outbox.get(ID).state == "approved"
    assert len([call for call in client.calls if call[0] == "verification"]) == 1
    assert service.outbox.get(ID).execution_code.endswith("_E1")


def test_station_correction_adds_classification_and_reuses_frozen_request_after_lost_receipt(local_record):
    service, report, review = local_record
    actual_station = "44444444-4444-4444-8444-444444444444"
    client = ArchiveClient()
    worker = worker_for(service, {**review, "station_id": actual_station, "bench": "B20"}, client)
    row = service.outbox.get(ID)
    service.outbox.enqueue({**row.manifest, **CTX, "recording_scope": "platform"}, report.bundle_path)
    client.lose_classification = True
    worker.process_once()
    assert service.outbox.get(ID).state == "sync_failed"
    create = next(call[1] for call in client.calls if call[0] == "create")
    assert create["classification"]["station_id"] == ID
    first_correction = next(call for call in client.calls if call[0] == "classification")
    assert first_correction[1]["context"] == {**CTX, "station_id": actual_station}
    assert not any(call[0] == "submit" for call in client.calls)
    service.outbox.retry(ID)
    worker.process_once()
    corrections = [call for call in client.calls if call[0] == "classification"]
    assert len(corrections) == 2 and corrections[0][1:] == corrections[1][1:]
    assert service.outbox.get(ID).state == "pending_review"
    submit = next(call for call in client.calls if call[0] == "submit")
    assert submit[1]["expected_revision"] == first_correction[1]["expected_revision"] + 1


def test_checksum_failure_retransmits_frozen_csv_then_completes_again(local_record):
    service, report, review = local_record
    class CorruptOnceClient(ArchiveClient):
        corrupt_once = True
        def archive_action(self, archive_id, action, payload, key):
            if action == "complete" and self.corrupt_once:
                self.corrupt_once = False
                self.calls.append((action, deepcopy(payload), key))
                self.detail.update(processing_state="FAILED", processing_error="CHECKSUM_MISMATCH", upload_state="CORRUPT", received_bytes=0)
                self.raw.clear()
                return deepcopy(self.detail)
            return super().archive_action(archive_id, action, payload, key)
    client = CorruptOnceClient()
    worker = worker_for(service, review, client)
    original = (report.execution_dir / "telemetry/raw.csv").read_bytes()
    worker.process_once()
    frozen = json.loads(service.outbox.get(ID).submission_payload)["create"]
    assert service.outbox.get(ID).state == "validating"
    service.outbox.update(ID, next_attempt_at=0)
    worker.process_once()
    assert service.outbox.get(ID).state == "awaiting_classification"
    chunks = [call for call in client.calls if call[0] == "chunk"]
    assert len(chunks) == 2 and chunks[0][1:] == chunks[1][1:]
    assert bytes(client.raw) == original
    completes = [call for call in client.calls if call[0] == "complete"]
    assert len(completes) == 2 and completes[0][1:] == completes[1][1:]
    assert len([call for call in client.calls if call[0] == "create"]) == 1
    assert json.loads(service.outbox.get(ID).submission_payload)["create"] == frozen


def test_parser_failure_stops_until_operator_explicitly_retries(local_record):
    service, _report, review = local_record
    class ParserFailureClient(ArchiveClient):
        repair_needed = True
        def archive_action(self, archive_id, action, payload, key):
            if action == "complete" and self.repair_needed:
                self.calls.append((action, deepcopy(payload), key))
                self.detail.update(processing_state="FAILED", processing_error="CSV_FORMAT_ERROR")
                return deepcopy(self.detail)
            return super().archive_action(archive_id, action, payload, key)
    client = ParserFailureClient()
    worker = worker_for(service, review, client)
    worker.process_once()
    assert service.outbox.get(ID).state == "processing_failed"
    assert worker.process_once() == 0
    before = len(client.calls)
    client.repair_needed = False
    service.retry_submission("synthetic-test")
    worker.process_once()
    assert service.outbox.get(ID).state == "awaiting_classification"
    assert not any(call[0] == "chunk" for call in client.calls[before:])
    assert len([call for call in client.calls if call[0] == "complete"]) == 2


def generic_record(tmp_path, legacy_passed=False):
    service = FactoryService(data_root=tmp_path / "data")
    source = tmp_path / "generic.csv"
    source.write_text("time_s,hip_cmd_pos_rad,hip_pos_rad,hip_torque_nm,hip_temp_c,hip_error_id\n0,0,0,1,20,0\n0.05,0,0,1,20,0\n0.1,0,0,1,20,0\n", encoding="utf-8")
    defaults = yaml.safe_load((Path(__file__).resolve().parents[1] / "factory_hmi/config/station_defaults.yaml").read_text(encoding="utf-8"))
    report = service.report_manager.finalize(source, {**CTX, "execution_uuid": ID, "test_id": "synthetic-test", "test_case_id": "REL-ARM-001",
        "test_name": "通用判据测试", "sample_id": "RP1.3-ARM-001", "actual_object": "RP1.3-ARM-001", "bench_id": "A10", "recording_scope": "platform",
        "started_at": "2026-10-15T00:00:00Z", "ended_at": "2026-10-15T00:00:00.1Z", "active_seconds": .1, "completed_cycles": 0,
        "outcome": "completed", "record_rate_hz": 20, "config": defaults})
    if legacy_passed:
        # An existing pre-fix local report can claim passed while central
        # typed criteria correctly identify the missing torque acceptance.
        summary = deepcopy(report.summary)
        summary["verdict"]["status"] = "passed"
        report.summary_path.write_text(json.dumps(summary), encoding="utf-8")
    review = {"tester": "实际测试人", "bench": "A10", "station_id": ID, "outcome": "成功", "notes": "无", "discrepancy_reason": "",
        "test_name": "通用判据测试", "actual_object": "RP1.3-ARM-001"}
    return service, report, review


class CentralUnconfiguredClient(ArchiveClient):
    def create_archive(self, payload, key):
        super().create_archive(payload, key)
        self.detail["manifest"] = {"analysis": {"version": "rp1-csv-analysis/1", "criteria_version": "generic-v1", "verdict": "unconfigured",
            "rules": [{"name": "hip_torque_nm.peak_abs", "result": "unconfigured"}]}}
        return deepcopy(self.detail)

    def archive_action(self, archive_id, action, payload, key):
        if action == "verification" and payload["verdict"] != "unconfigured" and not payload.get("discrepancy_reason"):
            self.calls.append((action, deepcopy(payload), key))
            raise PlatformError("人工结论与中央自动分析不同，必须填写原因", status=422)
        return super().archive_action(archive_id, action, payload, key)


def test_default_generic_rules_with_missing_rated_torque_do_not_pass(tmp_path):
    service, report, review = generic_record(tmp_path)
    assert report.summary["verdict"]["criteria_version"] == "generic-v1"
    assert report.summary["verdict"]["status"] == "unconfigured"
    assert any(rule["result"] == "not_evaluated" for rule in report.summary["verdict"]["rules"])
    with pytest.raises(ValueError, match="差异原因"):
        service.submit_execution("synthetic-test", review=review)
    client = CentralUnconfiguredClient()
    worker = worker_for(service, {**review, "discrepancy_reason": "额定力矩判据尚未配置，人工依据本次探索记录确认执行完成，未认定正式合格"}, client)
    worker.process_once()
    assert service.outbox.get(ID).state == "pending_review"


def test_central_mismatch_422_is_visible_and_manual_reverification_recovers(tmp_path):
    service, report, review = generic_record(tmp_path, legacy_passed=True)
    client = CentralUnconfiguredClient()
    worker = worker_for(service, review, client)
    worker.process_once()
    assert service.outbox.get(ID).state == "needs_verification"
    original_create = deepcopy(json.loads(service.outbox.get(ID).submission_payload)["create"])
    summary = service.execution_summary("synthetic-test")
    assert summary["central_analysis"]["verdict"] == "unconfigured"
    from factory_hmi.desktop.operator_review import analysis_text
    assert "中央自动判定：判据未配置完整" in analysis_text(summary)
    assert "中央与本机结论不同" in analysis_text(summary)
    with pytest.raises(ValueError, match="差异原因"):
        service.submit_execution("synthetic-test", review=review)
    reason = "中央额定力矩判据缺失；人工成功表示执行完成，本次未自动认定用例合格"
    service.submit_execution("synthetic-test", review={**review, "discrepancy_reason": reason})
    frozen = json.loads(service.outbox.get(ID).submission_payload)
    assert frozen["create"] == original_create and set(frozen) == {"create", "review"}
    assert service.execution_summary("synthetic-test")["central_analysis"]["verdict"] == "unconfigured"
    worker.process_once()
    assert service.outbox.get(ID).state == "pending_review"
    assert len([call for call in client.calls if call[0] == "create"]) == 1
    assert [call for call in client.calls if call[0] == "verification"][-1][1]["discrepancy_reason"] == reason
    assert json.loads(report.summary_path.read_text(encoding="utf-8"))["verdict"]["status"] == "passed"


def test_rejected_station_reverification_preserves_frozen_create_and_raw_hash(tmp_path):
    service, report, review = generic_record(tmp_path)
    client = CentralUnconfiguredClient()
    initial_review = {**review, "discrepancy_reason": "本次探索执行完成，判据待补齐"}
    worker = worker_for(service, initial_review, client)
    worker.process_once()
    original = deepcopy(json.loads(service.outbox.get(ID).submission_payload)["create"])
    assert original["station_label"] == "A10"
    client.receipt["status"] = "REJECTED"
    service.outbox.update(ID, next_attempt_at=0)
    worker.process_once()
    assert service.outbox.get(ID).state == "rejected"
    actual_station = "44444444-4444-4444-8444-444444444444"
    service.submit_execution("synthetic-test", review={**initial_review, "station_id": actual_station, "bench": "B20", "notes": "修正实际工位"})
    frozen = json.loads(service.outbox.get(ID).submission_payload)
    assert frozen["create"] == original and set(frozen) == {"create", "review"}
    worker.process_once()
    assert service.outbox.get(ID).state == "pending_review"
    assert len([call for call in client.calls if call[0] == "create"]) == 1
    correction = [call for call in client.calls if call[0] == "classification"][-1][1]
    assert correction["context"]["station_id"] == actual_station
    assert json.loads(service.outbox.get(ID).submission_payload)["create"]["sha256"] == original["sha256"]


def test_orphan_recovery_marks_clock_missing_without_using_filename_or_mtime(tmp_path):
    from factory_hmi.core.report import ReportManager
    root = tmp_path / "records"
    root.mkdir()
    csv = root / "20200101_personA_bench2.csv"
    csv.write_text("time_s,hip_pos_rad\n0,0\n1,1\n", encoding="utf-8")
    report, = ReportManager(tmp_path / "data").recover_orphans(root)
    assert report.manifest["origin"] == "HISTORY"
    assert report.manifest["timestamp_provenance"] == "missing"
    assert report.manifest["operator_id"] == ""
    assert report.manifest["started_at"] == "1970-01-01T00:00:00Z"
