#!/usr/bin/env python3
"""Exercise a deployed, isolated HTTPS platform using the actual HMI client.

This driver never connects to PostgreSQL, starts an API/worker, or accesses a
hardware backend. It uses a private scratch directory and synthetic samples.
Credentials are read from settings and generated browser accounts are written
with mode 0600. stdout contains only non-secret acceptance evidence.
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import ssl
import sys
import time
from urllib import error, parse, request
from uuid import uuid4


CONTEXT_FIELDS = ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")


class CheckFailure(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def write_private(path: Path, value):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def client_for(base_url, ca_file):
    from factory_hmi.sync.reliability_platform import ReliabilityPlatformClient

    client = ReliabilityPlatformClient(base_url)
    context = ssl.create_default_context(cafile=str(ca_file))
    require(context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED, "TLS verification is required")
    client._operator_opener = request.build_opener(
        request.HTTPCookieProcessor(client.cookies), request.HTTPSHandler(context=context))
    return client


def call(client, method, path, payload=None, *, expect="object", key=None):
    from factory_hmi.sync.uploader import PlatformError

    try:
        return client._call(method, path, payload=payload, expect=expect,
                            idempotency_key=key, service_auth=False)
    except PlatformError as exc:
        # Do not print a server response that could echo account/password input.
        raise CheckFailure(f"{method} {path.split('?', 1)[0]} returned status {exc.status}") from None


def status(client, method, path, payload=None):
    from factory_hmi.sync.uploader import PlatformError

    try:
        call_result = client._call(method, path, payload=payload, service_auth=False)
        return 200, call_result
    except PlatformError as exc:
        return exc.status, None


def raw_status(client, method, path, payload):
    """Real deployed HTTP status, bypassing only the HMI's local route guard."""
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    for cookie in client.cookies:
        if cookie.name == "rp1_csrf":
            headers["X-CSRF-Token"] = cookie.value
    message = request.Request(client.base_url + path, method=method,
                              data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with client._operator_opener.open(message, timeout=30) as response:
            return response.status
    except error.HTTPError as exc:
        return exc.code


def login(client, username, password, *, allow_initial_change=False):
    grant = call(client, "POST", "/api/v1/auth/login", {"username": username, "password": password})
    require(allow_initial_change or not grant["user"]["must_change_password"], "Acceptance account requires an initial password change")
    require(any(cookie.name == "rp1_session" and cookie.secure for cookie in client.cookies), "HTTPS secure session cookie missing")
    require(any(cookie.name == "rp1_csrf" for cookie in client.cookies), "CSRF cookie missing")
    return grant


def admin_login(client, settings):
    grant = login(client, settings["admin_username"], settings["admin_password"], allow_initial_change=True)
    if grant["user"]["must_change_password"]:
        new_password = settings.get("pending_admin_password") or "Synthetic-Admin!" + secrets.token_urlsafe(24)
        settings["pending_admin_password"] = new_password
        target = Path(settings["_settings_path"])
        write_private(target, {key: value for key, value in settings.items() if not key.startswith("_")})
        call(client, "POST", "/api/v1/auth/change-password", {
            "current_password": settings["admin_password"], "new_password": new_password})
        settings["admin_password"] = new_password
        settings.pop("pending_admin_password", None)
        write_private(target, {key: value for key, value in settings.items() if not key.startswith("_")})
        login(client, settings["admin_username"], settings["admin_password"])


def setup(admin, path, payload):
    return call(admin, "POST", "/api/v1/setup/" + path, {**payload, "reason": "Synthetic isolated deployment acceptance"})


def create_context(admin, executor_id):
    tag = uuid4().hex[:8].upper()
    options = call(admin, "GET", "/api/v1/setup/options")
    sequence = len(options["assets"]) + 1
    require(sequence < 1000, "Synthetic sample numbering exhausted")
    sample = setup(admin, "assets", {
        "asset_code": f"RP1.3-SARM-{sequence:03d}", "asset_kind": "MODULE", "target_part_code": "SARM",
        "product_family": "SYNTHETIC-QA", "serial_number": uuid4().hex, "display_name": "合成验收对象",
        "model": "Synthetic", "configuration_fingerprint": "QA-" + tag})
    program = setup(admin, "programs", {"program_code": "QA-" + tag, "name": "隔离部署验收项目"})
    plan = setup(admin, "campaigns", {"program_id": program["id"], "campaign_code": "QA-" + tag,
                                     "name": "隔离部署验收计划", "asset_kind": "MODULE"})
    station = setup(admin, "stations", {
        "site_code": "QA-" + tag, "site_name": "Synthetic QA", "lab_code": "QA-" + tag, "lab_name": "Synthetic QA",
        "station_code": "QA-" + tag, "name": "隔离模拟工位", "station_type": "MODULE"})
    catalog = call(admin, "GET", "/api/v1/setup/catalog", expect="list")
    case = next(item for item in catalog if item["target_part_code"] == "ARM" and item["enabled"] and item["version_id"])
    context = setup(admin, f"campaigns/{plan['id']}/contexts", {
        "asset_id": sample["id"], "configuration_id": sample["configuration_id"],
        "test_case_version_id": case["version_id"], "station_id": station["id"], "cycle_code": "QA-" + tag})["context"]
    call(admin, "POST", f"/api/v1/admin/users/{executor_id}/campaign-grants", {
        "campaign_id": plan["id"], "access_level": "EDIT", "reason": "Synthetic isolated acceptance permission"})
    return context


def account(admin, role, tag):
    username = "qa-" + role.lower() + "-" + tag
    password = "Synthetic-QA!" + secrets.token_urlsafe(24)
    user = call(admin, "POST", "/api/v1/admin/users", {
        "username": username, "password": password, "display_name": "合成验收 " + role,
        "role": role, "must_change_password": False})
    return {"username": username, "password": password, "id": user["id"]}


def review_fields(profile, *, failed=False, label="Synthetic deployment run"):
    return {"tester": "合成测试员", "bench": profile["bench_id"], "station_id": profile["station_id"],
            "outcome": "失败" if failed else "成功", "notes": "纯合成隔离验收记录；未连接真实 PLC/CAN。",
            "test_name": label, "actual_object": profile["sample_id"],
            "discrepancy_reason": "合成验收明确核实人工结论；未配置数值判据不能自动判定通过。"}


def fake_record(scratch, profile):
    """Record an actual FakeMotorBackend playback through FactoryService."""
    import numpy as np
    import factory_hmi
    from factory_hmi.core.service import FactoryService

    root = scratch / "fake-motor-run"
    service = FactoryService(data_root=root)
    hmi_root = Path(factory_hmi.__file__).resolve().parents[1]
    service.configure(config_path=str(hmi_root / "scripts/config/arm_motors.yaml"), limb="left_arm", backend="fake")
    timeline = np.arange(181, dtype=float) / 60
    trajectory = root / "synthetic-trajectory.npz"
    np.savez(trajectory, motor_pos=np.repeat((.05 * np.sin(timeline * 2))[:, None], 7, axis=1),
             fps=np.asarray([60.]), time=timeline, limb=np.asarray(["left_arm"]))
    service.import_trajectory(str(trajectory))
    try:
        service.connect()  # FakeMotorBackend: fake CAN state only, no socket/native driver.
        service.arm()
        service.start(loop=False, record=True, record_rate_hz=20, recording_scope="platform",
                      robot_id=profile["sample_id"], bench_id=profile["bench_id"],
                      test_case_id=profile["test_case_id"], test_name="A 模拟电机完整采集",
                      **{field: profile[field] for field in CONTEXT_FIELDS})
        require(service.controller.wait(timeout=15), "Fake motor playback did not finish")
        for thread in list(service._report_threads):
            thread.join(timeout=20)
            require(not thread.is_alive(), "Local report finalization did not finish")
        require(not service._report_errors, "Local fake-device report failed")
        history = service.execution_history()
        require(len(history) == 1, "Fake motor recording must produce one immutable execution")
        test_id = history[0]["test_id"]
        manifest = service.report_manager.manifest(test_id)
        require(manifest["machine_status"] == "completed", "Fake motor completion did not remain a machine fact")
        raw_path = service.report_manager.execution_dir(test_id) / "telemetry/raw.csv"
        require(raw_path.is_file() and raw_path.stat().st_size > 0, "Fake motor CSV missing")
        return service, test_id, raw_path, manifest
    finally:
        service.disconnect()


def synthetic_record(scratch, profile, label, *, origin="OTHER", failed=False, ordinal=1, sample_count=12001):
    """Explicit synthetic samples, including a known spike, never real CSV."""
    from factory_hmi.core.service import FactoryService
    import factory_hmi
    import yaml

    root = scratch / ("synthetic-" + label)
    service = FactoryService(data_root=root)
    source = root / "synthetic.csv"
    with source.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["time_s", "hip_cmd_pos_rad", "hip_pos_rad", "hip_torque_nm", "hip_temp_c", "hip_error_id"])
        for index in range(sample_count):
            seconds = index / 200
            position = round(.2 * math.sin(seconds / 2), 6)
            writer.writerow([seconds, position, round(position + ordinal * .001, 6),
                             12 if index == 7200 else round(math.sin(seconds) * 2, 6),
                             round(25 + seconds / 20, 4), 1 if failed and index == sample_count - 1 else 0])
    config = yaml.safe_load((Path(factory_hmi.__file__).parent / "config/station_defaults.yaml").read_text(encoding="utf-8"))
    end = datetime.now(timezone.utc) - timedelta(minutes=10 + ordinal * 3)
    duration = (sample_count - 1) / 200
    test_id = "synthetic-deployment-" + label + "-" + uuid4().hex
    initial = {
        **{field: profile[field] for field in CONTEXT_FIELDS}, "execution_uuid": str(uuid4()), "test_id": test_id,
        "test_case_id": profile["test_case_id"] if origin == "HMI" else "OTHER",
        "sample_id": profile["sample_id"], "bench_id": profile["bench_id"],
        "recording_scope": "platform" if origin == "HMI" else "local", "test_name": label + " 合成曲线验收",
        "actual_object": profile["sample_id"], "started_at": (end - timedelta(seconds=duration)).isoformat(), "ended_at": end.isoformat(),
        "active_seconds": duration, "completed_cycles": 1, "record_rate_hz": 200,
        "outcome": "failed" if failed else "completed", "origin": origin, "config": config,
    }
    report = service.report_manager.finalize(source, initial)
    return service, test_id, report.execution_dir / "telemetry/raw.csv", report.manifest


def drive(worker, service, execution_uuid, expected, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = service.outbox.get(execution_uuid)
        if item.state == expected:
            return item
        require(item.state not in {"needs_verification", "processing_failed"},
                f"HMI queue requires intervention: {item.state}")
        service.outbox.update(execution_uuid, next_attempt_at=0)
        worker.process_once()
        time.sleep(.5)  # Processing is performed by the separately deployed archive-worker.
    item = service.outbox.get(execution_uuid)
    raise CheckFailure(f"HMI queue timeout: expected {expected}, received {item.state}")


def enqueue(worker, record, profile, username, expected, *, failed=False, label="Synthetic deployment run"):
    service, test_id, raw_path, manifest = record
    worker.outbox = service.outbox
    require(service.outbox.list() == [], "New recording unexpectedly queued before explicit verification")
    try:
        service.submit_execution(test_id)
    except ValueError:
        pass
    else:
        raise CheckFailure("Submission without operator verification was accepted")
    row = service.submit_execution(test_id, review=review_fields(profile, failed=failed, label=label))
    service.outbox.update(row["execution_uuid"], submission_owner=username)
    item = drive(worker, service, row["execution_uuid"], expected)
    detail = worker.client.archive(item.upload_id)
    raw = raw_path.read_bytes()
    require(detail["sha256"] == hashlib.sha256(raw).hexdigest(), "Archive evidence SHA256 differs from local retained CSV")
    require(detail["received_bytes"] == len(raw) == detail["size_bytes"], "Archive evidence byte count differs")
    require(detail["processing_state"] == "READY", "Independent archive worker did not reach READY")
    require(raw_path.read_bytes() == raw, "Local CSV copy changed after upload")
    return detail, {"archive_id": detail["id"], "execution_uuid": row["execution_uuid"], "test_id": test_id,
                    "sha256": detail["sha256"], "size_bytes": len(raw), "local_csv": str(raw_path),
                    "origin": detail["origin"], "source": "FakeMotorBackend" if label.startswith("A ") else "synthetic CSV",
                    "submission_id": detail["submission_id"]}


def decide(client, archive, decision="APPROVED"):
    receipt = call(client, "GET", f"/api/v1/submissions/{archive['submission_id']}")
    return call(client, "POST", f"/api/v1/submissions/{archive['submission_id']}/review", {
        "revision": receipt["revision"], "decision": decision, "reason": "合成隔离部署验收自审 " + decision}, key=uuid4().hex)


def assert_hidden(viewer, archive):
    require(status(viewer, "GET", f"/api/v1/archives/{archive['id']}")[0] in {403, 404}, "Viewer can read unapproved/unclassified archive")
    require(status(viewer, "GET", f"/api/v1/archives/{archive['id']}/curves?metrics=pos_rad")[0] in {403, 404}, "Viewer can read unapproved/unclassified curves")
    require(archive["execution_id"] is None, "Unapproved submission unexpectedly published an execution")


def curves(client, reference, *, spike=False):
    metric = "torque_nm" if spike else "pos_rad"
    data = call(client, "GET", f"/api/v1/archives/{reference}/curves?metrics={metric}&max_points=1000")
    require(bool(data["series"]) and bool(data["series"][0]["points"]), "Approved archive curve is empty")
    if spike:
        require(any(point[1] == 12 for series in data["series"] for point in series["points"]), "Curve downsampling lost the known 12 Nm spike")
        window = call(client, "GET", f"/api/v1/archives/{reference}/curves?metrics=torque_nm&start=35&end=37&max_points=1000")
        require(any(point[1] == 12 for series in window["series"] for point in series["points"]), "Viewport query lost the known spike")
        require(all(35 <= point[0] <= 37 for series in window["series"] for point in series["points"]), "Viewport query returned out-of-window samples")
    return {"metric": metric, "series_count": len(data["series"]), "returned_points": sum(len(series["points"]) for series in data["series"]),
            "curve_sha256": hashlib.sha256(json.dumps(data["series"], sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def bootstrap(base_url, settings, scratch):
    if (scratch / "deployment-seed-context.json").exists():
        return (json.loads((scratch / "browser-accounts.json").read_text(encoding="utf-8")),
                json.loads((scratch / "deployment-seed-context.json").read_text(encoding="utf-8")))
    admin = client_for(base_url, settings["ca_file"])
    admin_login(admin, settings)
    tag = uuid4().hex[:10]
    accounts = {"base_url": base_url, "executor": account(admin, "TEST_EXECUTOR", tag), "viewer": account(admin, "VIEWER", tag)}
    write_private(scratch / "browser-accounts.json", accounts)
    context = create_context(admin, accounts["executor"]["id"])
    write_private(scratch / "deployment-seed-context.json", context)
    return accounts, context


def uploader(base_url, settings, scratch, accounts, data_root=None):
    from factory_hmi.sync.reliability_platform import ReliabilityUploaderWorker
    from factory_hmi.sync.outbox import Outbox

    executor = client_for(base_url, settings["ca_file"])
    worker = ReliabilityUploaderWorker(Outbox(data_root / "sync/outbox.sqlite3" if data_root else scratch / "bootstrap.sqlite3"), executor)
    worker.authenticate(accounts["executor"]["username"], accounts["executor"]["password"])
    return worker


def queue(base_url, settings, scratch):
    """Lose the first successful 4 MiB receipt, persist queue, then exit."""
    from factory_hmi.sync.uploader import PlatformError

    require(not (scratch / "deployment-resume.json").exists(), "Queue evidence already exists; use fresh scratch")
    accounts, context = bootstrap(base_url, settings, scratch)
    worker = uploader(base_url, settings, scratch, accounts)
    profile = next(item for item in worker.context_profiles() if item["campaign_id"] == context["campaign_id"])
    service, test_id, raw_path, manifest = synthetic_record(scratch, profile, "E", ordinal=5, sample_count=160001)
    require(raw_path.stat().st_size > 5 * 1024**2, "Resume sample must exceed 5 MiB")
    worker.outbox = service.outbox
    row = service.submit_execution(test_id, review=review_fields(profile, label="E 多块中断恢复验收"))
    identity = row["execution_uuid"]
    service.outbox.update(identity, submission_owner=accounts["executor"]["username"])
    original = worker.client._call
    receipt_lost = False

    def lose_first_chunk(method, path, **kwargs):
        nonlocal receipt_lost
        value = original(method, path, **kwargs)
        if method == "PUT" and "/content?offset=0" in path and not receipt_lost:
            receipt_lost = True
            raise PlatformError("Synthetic first chunk committed; client lost receipt", status=503)
        return value

    worker.client._call = lose_first_chunk
    worker.process_once()
    worker.client._call = original
    require(receipt_lost, "Synthetic receipt loss was not reached")
    item = service.outbox.get(identity)
    require(item.state == "sync_failed", "Interrupted HMI upload was not persisted for retry")
    detail = worker.client.archive(identity)
    require(detail["received_bytes"] == 4 * 1024**2 and detail["upload_state"] == "UPLOADING", "First 4 MiB chunk did not commit")
    evidence = {"archive_id": detail["id"], "execution_uuid": identity, "test_id": test_id, "local_csv": str(raw_path),
                "sha256": detail["sha256"], "size_bytes": detail["size_bytes"], "origin": "OTHER", "source": "synthetic CSV",
                "interrupted_offset": detail["received_bytes"], "local_state": item.state, "status": "INTERRUPTED"}
    write_private(scratch / "deployment-resume.json", evidence)
    return evidence


def resume(base_url, settings, scratch):
    """Reopen durable HMI state; worker is deliberately stopped by the runner."""
    from factory_hmi.core.service import FactoryService

    evidence = json.loads((scratch / "deployment-resume.json").read_text(encoding="utf-8"))
    accounts, _ = bootstrap(base_url, settings, scratch)
    service = FactoryService(data_root=scratch / "synthetic-E")
    worker = uploader(base_url, settings, scratch, accounts, data_root=service.data_root)
    before = worker.client.archive(evidence["archive_id"])
    require(before["received_bytes"] == evidence["interrupted_offset"], "Restart lost the committed byte offset")
    # Observe only routing and byte counts; cookies and headers are never saved.
    chunks = []
    original = worker.client._call

    def observe(method, path, **kwargs):
        value = original(method, path, **kwargs)
        if method == "PUT" and "/content?offset=" in path:
            chunks.append({"offset": int(parse.parse_qs(parse.urlparse(path).query)["offset"][0]), "size": len(kwargs["body"])})
        return value

    worker.client._call = observe
    service.outbox.update(evidence["execution_uuid"], next_attempt_at=0)
    worker.process_once()
    worker.client._call = original
    detail = worker.client.archive(evidence["archive_id"])
    require(chunks and chunks[0]["offset"] == 4 * 1024**2, "HMI did not resume from the committed offset")
    require(detail["received_bytes"] == evidence["size_bytes"], "Resumed CSV is incomplete")
    require(detail["processing_state"] in {"QUEUED", "PROCESSING", "READY"}, "Complete did not queue the independent processor")
    evidence.update(resumed_chunks=chunks, received_bytes=detail["received_bytes"], processing_state=detail["processing_state"],
                    status="RESUMED", local_state=service.outbox.get(evidence["execution_uuid"]).state)
    write_private(scratch / "deployment-resume.json", evidence)
    return evidence


def seed(base_url, settings, scratch):
    from factory_hmi.core.service import FactoryService

    require(not (scratch / "deployment-result.json").exists(), "Seed is not repeatable over existing acceptance evidence; use a fresh isolated scratch")
    accounts, context = bootstrap(base_url, settings, scratch)
    admin = client_for(base_url, settings["ca_file"])
    admin_login(admin, settings)
    for path in ("executions", "executions/obsolete/finish", "executions/by-producer/obsolete/telemetry"):
        endpoint = "/api/v1/ingestion/" + path
        require(status(admin, "POST", endpoint, {})[0] == 410, "HMI local legacy guard is missing")
        require(raw_status(admin, "POST", endpoint, {}) == 404, "Deployed legacy ingestion route is exposed")
    worker = uploader(base_url, settings, scratch, accounts)
    executor = worker.client
    profile = next(item for item in worker.context_profiles() if item["campaign_id"] == context["campaign_id"])
    viewer = client_for(base_url, settings["ca_file"])
    login(viewer, accounts["viewer"]["username"], accounts["viewer"]["password"])
    results = {}

    if (scratch / "deployment-resume.json").exists():
        recovery = json.loads((scratch / "deployment-resume.json").read_text(encoding="utf-8"))
        service = FactoryService(data_root=scratch / "synthetic-E")
        worker.outbox = service.outbox
        item = drive(worker, service, recovery["execution_uuid"], "awaiting_classification")
        recovered = executor.archive(item.upload_id)
        assert_hidden(viewer, recovered)
        require(recovered["sha256"] == recovery["sha256"], "Resumed CSV hash changed")
        recovery.update(status="UNCLASSIFIED", curve=curves(executor, recovered["id"], spike=True))
        results["E"] = recovery

    first_record = fake_record(scratch, profile)
    first, results["A"] = enqueue(worker, first_record, profile, accounts["executor"]["username"],
                                   "pending_review", label="A 模拟电机完整采集")
    assert_hidden(viewer, first)
    require(call(executor, "GET", f"/api/v1/assets/{context['asset_id']}/executions")["items"] == [],
            "Pending archive entered the formal asset execution list")
    require(status(executor, "GET", f"/api/v1/executions/{first['execution_uuid']}")[0] == 404,
            "Pending archive is accessible through the formal execution endpoint")
    # A missing CSRF header must be rejected through the deployed reverse proxy.
    csrf = next(cookie for cookie in executor.cookies if cookie.name == "rp1_csrf")
    executor.cookies.clear(csrf.domain, csrf.path, csrf.name)
    code, _ = status(executor, "POST", f"/api/v1/submissions/{first['submission_id']}/review", {
        "revision": first["submission_revision"], "decision": "APPROVED", "reason": "Synthetic CSRF rejection"})
    require(code == 403, "Deployed approval endpoint accepted a request without CSRF")
    worker.authenticate(accounts["executor"]["username"], accounts["executor"]["password"])
    approved = decide(executor, first)
    require(approved["status"] == "APPROVED", "Executor self approval failed")
    drive(worker, first_record[0], first["execution_uuid"], "approved")
    first = executor.archive(first["id"])
    results["A"].update(execution_id=first["execution_id"], execution_code=first["execution_code"], status="APPROVED",
                         curve=curves(viewer, first["execution_id"]))

    second_record = synthetic_record(scratch, profile, "B", failed=True, ordinal=1)
    second, results["B"] = enqueue(worker, second_record, profile, accounts["executor"]["username"], "awaiting_classification", failed=True, label="B 失败测试后补归类")
    require(second["classification_state"] == "UNCLASSIFIED" and second["submission_id"] is None, "OTHER bypassed formal classification")
    curves(executor, second["id"], spike=True)
    assert_hidden(viewer, second)
    second = executor.archive_action(second["id"], "classification", {
        "expected_revision": second["revision"], "context": context, "reason": "Synthetic OTHER late classification"}, uuid4().hex)
    second = executor.archive_action(second["id"], "submit", {
        "expected_revision": second["revision"], "expected_submission_revision": 0, "reason": "Synthetic first submission"}, uuid4().hex)
    assert_hidden(viewer, second)
    rejected = decide(executor, second, "REJECTED")
    require(rejected["status"] == "REJECTED" and rejected["execution_id"] is None, "Rejected data was published")
    second = executor.archive(second["id"])
    assert_hidden(viewer, second)
    old_facts = second["facts"]
    old_verification = second["current_verification"]
    second = executor.archive_action(second["id"], "verification", {
        "expected_revision": second["revision"], "tester": "修正后的合成测试员", "station_id": profile["station_id"],
        "verdict": "failed", "notes": "驳回后补充备注：合成末帧包含故障码，原始 CSV 不变。",
        "discrepancy_reason": "合成失败测试人工核实", "reason": "Synthetic rejection correction appended"}, uuid4().hex)
    require(second["facts"] == old_facts and len(second["verification_history"]) == 2, "Verification correction changed measurements or discarded history")
    require(second["verification_history"][0]["payload"]["tester"] == old_verification["tester"], "Original tester evidence was overwritten")
    second = executor.archive_action(second["id"], "submit", {
        "expected_revision": second["revision"], "expected_submission_revision": 1, "reason": "Synthetic corrected resubmission"}, uuid4().hex)
    require(second["submission_revision"] == 2, "Resubmission did not append revision 2")
    approved = decide(executor, second)
    require(approved["status"] == "APPROVED", "Corrected OTHER approval failed")
    second = executor.archive(second["id"])
    results["B"].update(submission_id=second["submission_id"], execution_id=second["execution_id"], execution_code=second["execution_code"],
                         status="APPROVED", verification_versions=2, submission_versions=2, curve=curves(viewer, second["execution_id"], spike=True))

    third, results["C"] = enqueue(worker, synthetic_record(scratch, profile, "C", ordinal=2), profile,
                                   accounts["executor"]["username"], "awaiting_classification", label="C 未归类曲线隔离")
    assert_hidden(viewer, third)
    results["C"].update(status="UNCLASSIFIED", curve=curves(executor, third["id"], spike=True))
    fourth, results["D"] = enqueue(worker, synthetic_record(scratch, profile, "D", origin="HMI", ordinal=3), profile,
                                    accounts["executor"]["username"], "pending_review", label="D 浏览器审批验收")
    assert_hidden(viewer, fourth)
    results["D"].update(status="PENDING", submission_revision=fourth["submission_revision"])
    formal = call(executor, "GET", f"/api/v1/assets/{context['asset_id']}/executions")["items"]
    require({item["id"] for item in formal} == {results["A"]["execution_id"], results["B"]["execution_id"]},
            "Unapproved/unclassified archive entered formal queries")
    require(status(viewer, "POST", "/api/v1/archives", {})[0] == 403, "Viewer can create archives")
    evidence = {"format": 1, "base_url": base_url, "created_at": datetime.now(timezone.utc).isoformat(), "context": context,
                "records": results, "checks": {"verified_https": True, "independent_worker_ready": True, "csrf_rejected": True,
                "self_review": True, "other_late_classification": True, "rejection_append_resubmit": True,
                "local_copy_retained": True, "viewer_isolation": True, "legacy_ingestion_closed": True,
                "durable_chunk_resume": "E" in results, "formal_query_gate": True}}
    write_private(scratch / "deployment-result.json", evidence)
    return evidence


def conflict(base_url, settings, scratch):
    """Create a real differing human verdict on the synthetic pending sentinel."""
    evidence = json.loads((scratch / "deployment-result.json").read_text(encoding="utf-8"))
    accounts = json.loads((scratch / "browser-accounts.json").read_text(encoding="utf-8"))
    executor = client_for(base_url, settings["ca_file"])
    login(executor, accounts["executor"]["username"], accounts["executor"]["password"])
    item = executor.archive(evidence["records"]["D"]["archive_id"])
    require(item["submission_status"] == "PENDING", "Conflict sentinel must still be pending")
    original_facts, original_sha = item["facts"], item["sha256"]
    require(original_facts["machine_status"] == "completed", "Conflict sentinel must be machine-completed")
    decide(executor, item, "REJECTED")
    item = executor.archive(item["id"])
    verification = item["current_verification"]
    item = executor.archive_action(item["id"], "verification", {
        "expected_revision": item["revision"], "tester": verification["tester"], "station_id": verification["station_id"],
        "verdict": "blocked", "notes": "合成验收：机器已完成，但数值判据未配置，人工保留待判定；原始数据不变。",
        "discrepancy_reason": "合成冲突结论显示验收", "reason": "Synthetic differing human verdict appended"}, uuid4().hex)
    item = executor.archive_action(item["id"], "submit", {
        "expected_revision": item["revision"], "expected_submission_revision": item["submission_revision"],
        "reason": "Synthetic differing human verdict resubmission"}, uuid4().hex)
    require(item["facts"] == original_facts and item["sha256"] == original_sha, "Human correction changed acquisition evidence")
    receipt = executor.submission(item["submission_id"])
    summary = receipt["revisions"][-1]["summary"]
    require(summary["source_status"] == "passed" and item["current_verification"]["verdict"] == "blocked", "Conflict fixture did not preserve distinct values")
    evidence["records"]["D"].update(submission_revision=item["submission_revision"], status="PENDING")
    write_private(scratch / "deployment-result.json", evidence)
    result = {"archive_id": item["id"], "submission_id": item["submission_id"], "machine_status": original_facts["machine_status"],
              "summary_source_status": summary["source_status"], "human_verdict": "blocked", "sha256": original_sha,
              "acquisition_unchanged": True, "submission_revision": item["submission_revision"]}
    write_private(scratch / "human-machine-conflict.json", result)
    return result


def verify(base_url, settings, scratch):
    evidence = json.loads((scratch / "deployment-result.json").read_text(encoding="utf-8"))
    accounts = json.loads((scratch / "browser-accounts.json").read_text(encoding="utf-8"))
    executor, viewer = (client_for(base_url, settings["ca_file"]) for _ in range(2))
    login(executor, accounts["executor"]["username"], accounts["executor"]["password"])
    login(viewer, accounts["viewer"]["username"], accounts["viewer"]["password"])
    checked = {}
    for key, item in evidence["records"].items():
        detail = executor.archive(item["archive_id"])
        require(detail["sha256"] == item["sha256"] and detail["size_bytes"] == item["size_bytes"], "Restored archive identity/hash differs")
        require(detail["processing_state"] == "READY", "Restored archive is not queryable")
        require(hashlib.sha256(Path(item["local_csv"]).read_bytes()).hexdigest() == item["sha256"], "Local immutable evidence differs after restart/restore")
        if key in {"A", "B"}:
            require(detail["submission_status"] == "APPROVED" and detail["execution_id"] == item["execution_id"], "Restored approved execution differs")
            call(viewer, "GET", f"/api/v1/executions/{item['execution_id']}")
            query = curves(viewer, item["execution_id"], spike=key == "B")
            require(query["curve_sha256"] == item["curve"]["curve_sha256"], "Curve content differs from pre-restart evidence")
        elif key in {"C", "E"}:
            require(detail["classification_state"] == "UNCLASSIFIED", "Unclassified sentinel was unexpectedly classified")
            assert_hidden(viewer, detail)
        else:
            # The real browser may approve D after seed; verify both allowed states.
            require(detail["submission_status"] in {"PENDING", "APPROVED"}, "Browser acceptance submission state invalid")
            if detail["submission_status"] == "PENDING":
                assert_hidden(viewer, detail)
            else:
                curves(viewer, detail["execution_id"], spike=True)
        checked[key] = {"archive_id": item["archive_id"], "sha256": detail["sha256"], "processing_state": detail["processing_state"],
                        "classification_state": detail["classification_state"], "submission_status": detail["submission_status"]}
    result = {"base_url": base_url, "verified_at": datetime.now(timezone.utc).isoformat(), "records": checked, "passed": True}
    write_private(scratch / "deployment-verify-result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--settings-file", type=Path, required=True)
    parser.add_argument("--phase", choices=("queue", "resume", "seed", "conflict", "verify"), required=True)
    args = parser.parse_args()
    url = parse.urlparse(args.base_url)
    require(url.scheme == "https" and url.hostname in {"localhost", "127.0.0.1", "::1"}, "Only an isolated local HTTPS deployment is allowed")
    settings = json.loads(args.settings_file.read_text(encoding="utf-8"))
    settings["_settings_path"] = str(args.settings_file)
    scratch = Path(settings["scratch_dir"]).resolve(strict=True)
    require(scratch.is_relative_to(Path("/tmp")) and scratch != Path("/tmp"), "A private /tmp scratch directory is required")
    require(Path(settings["ca_file"]).is_file(), "Synthetic CA certificate missing")
    phase = {"queue": queue, "resume": resume, "seed": seed, "conflict": conflict, "verify": verify}[args.phase]
    result = phase(args.base_url.rstrip("/"), settings, scratch)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (CheckFailure, OSError, ValueError, KeyError, StopIteration) as exc:
        # Exception text from product code is intentionally excluded: credentials
        # and cookie values must never be copied into command output.
        message = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        print(json.dumps({"passed": False, "error": message}), file=sys.stderr)
        raise SystemExit(1)
