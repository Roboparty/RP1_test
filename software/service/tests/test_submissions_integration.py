"""Approval behavior through real cookies, RLS, concurrent transactions and read models."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.repositories.mtbf import MtbfRepository
from app.schemas.mtbf import CampaignMtbfConfigRequest
from app.services.worker import claim_job, process_job

from test_hmi_presence_integration import environment, signed_in
from test_setup_integration import asset, data, post
from test_submissions import summary

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated database required")]


def submit(client, payload, revision=0, key=None):
    prepared = prepare_archive(client, payload)
    if prepared is not None:
        return prepared
    return client.post("/api/v1/submissions", json={"summary": payload, "expected_revision": revision, "reason": "Synthetic test submission"}, headers={"Idempotency-Key": key or uuid4().hex})


def prepare_archive(client, payload):
    """Exercise real archive prerequisites; never bypass the publication gate in QA."""
    from test_archives import archive_payload
    from test_archives_integration import RAW, headers, upload, verify
    current = client.get(f"/api/v1/archives/{payload['execution_uuid']}")
    if current.status_code == 404:
        fields = ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")
        facts = {k: payload[k] for k in ("started_at", "ended_at", "active_seconds", "completed_cycles", "data_quality", "clock_quality", "exception_count", "summary")}
        facts.update(machine_status={"passed": "completed", "failed": "failed", "blocked": "interrupted"}[payload["source_status"]], automatic_verdict="unconfigured")
        request = archive_payload(context={k: payload[k] for k in fields}, raw=RAW, facts=facts)
        request["execution_uuid"] = payload["execution_uuid"]
        created = client.post("/api/v1/archives", json=request, headers=headers())
        if created.status_code != 201:
            return created
        item = upload(client, created.json()["data"])
        checked = verify(client, item, payload["station_id"], payload["source_status"])
        if checked.status_code != 200:
            return checked
        item = checked.json()["data"]
        payload["data_quality"] = item["manifest"]["quality_status"] if item["facts"]["data_quality"] == "VALID" else item["facts"]["data_quality"]
    elif current.status_code == 200:
        item = current.json()["data"]
    else:
        return current
    if payload["evidence_sha256"] == "a" * 64:
        payload["evidence_sha256"] = item["sha256"]
    return None


def review(client, item, decision="APPROVED", key=None):
    return client.post(f"/api/v1/submissions/{item['id']}/review", json={"revision": item["revision"], "decision": decision, "reason": "Synthetic review decision"}, headers={"Idempotency-Key": key or uuid4().hex})


def context(admin):
    tag = uuid4().hex[:8].upper()
    # Unique sample code within every isolated run.
    sequence = len(admin.get("/api/v1/setup/options").json()["data"]["assets"]) + 1
    sample = post(admin, "assets", asset(f"RP1.3-SLEG-{sequence:03d}"))
    program = post(admin, "programs", {"program_code": "AP-" + tag, "name": "Approval QA"})
    plan = post(admin, "campaigns", {"program_id": program["id"], "campaign_code": "AP-" + tag, "name": "Approval QA", "asset_kind": "MODULE"})
    station = post(admin, "stations", {"site_code": "AP-" + tag, "site_name": "Approval QA", "lab_code": "AP-" + tag, "lab_name": "Approval QA", "station_code": "AP-" + tag, "name": "Approval QA", "station_type": "MODULE"})
    # Reduced-degree samples use the published full-leg catalog after 0034.
    case = next(c for c in data(admin, "setup/catalog") if c["target_part_code"] == "LEG" and c["enabled"] and c["version_id"])
    ctx = post(admin, f"campaigns/{plan['id']}/contexts", {"asset_id": sample["id"], "configuration_id": sample["configuration_id"], "test_case_version_id": case["version_id"], "station_id": station["id"], "cycle_code": "AP-" + tag})["context"]
    return {**summary(), **ctx}


def grant(admin, engine, username, campaign_id, level="EDIT"):
    with engine.connect() as conn:
        user_id = conn.execute(text("SELECT public_id FROM iam.app_user WHERE username=:name"), {"name": username}).scalar_one()
    r = admin.post(f"/api/v1/admin/users/{user_id}/campaign-grants", json={"campaign_id": campaign_id, "access_level": level, "reason": "Synthetic approval permissions"})
    assert r.status_code == 200, r.text


def mtbf_seconds(engine, username, campaign_id):
    with engine.begin() as conn:
        actor = conn.execute(text("SELECT public_id FROM iam.app_user WHERE username=:name"), {"name": username}).scalar_one()
        conn.execute(text("SELECT iam.set_request_context(:actor,'approval-qa-worker','synthetic isolated calculation')"), {"actor": actor})
        session = Session(bind=conn)
        MtbfRepository(session).upsert_config(UUID(campaign_id), "OVERALL", CampaignMtbfConfigRequest(target_hours=1000), None)
        conn.execute(text("SELECT reliability.enqueue_campaign_recompute(c.id,s.id,'approval-qa',NULL) FROM test.test_campaign c JOIN reliability.mtbf_scope s ON s.asset_kind=c.asset_kind AND s.scope_code='OVERALL' AND s.status='PUBLISHED' WHERE c.public_id=:id"), {"id": campaign_id})
        job = claim_job(session, "approval-qa-worker")
        assert job is not None
        process_job(session, job)
        return float(conn.execute(text("SELECT r.exposure_seconds FROM reliability.current_mtbf_result r JOIN test.test_campaign c ON c.id=r.campaign_id WHERE c.public_id=:id"), {"id": campaign_id}).scalar_one())


def test_pending_rejected_isolation_resubmission_atomic_publication_and_audit(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as author, signed_in(users["second"]) as stranger, signed_in(users["viewer"]) as viewer:
        payload = context(admin)
        grant(admin, engine, users["first"], payload["campaign_id"])
        grant(admin, engine, users["viewer"], payload["campaign_id"], "VIEW")
        assert submit(viewer, payload).status_code == 403
        key = uuid4().hex
        response = submit(author, payload, key=key)
        assert response.status_code == 201, response.text
        pending = response.json()["data"]
        assert pending["status"] == "PENDING" and pending["execution_id"] is None
        listed = admin.get("/api/v1/submissions?status=PENDING")
        assert listed.status_code == 200, listed.text
        assert any(item["id"] == pending["id"] for item in listed.json()["data"]["items"])
        assert pending["asset_code"].startswith("RP1.3-SLEG-") and pending["case_code"].startswith("REL-LEG-")
        assert pending["station_code"].startswith("AP-")
        assert submit(author, payload, key=key).json()["data"]["id"] == pending["id"]
        assert submit(author, {**payload, "active_seconds": 40}, key=key).status_code == 409
        assert stranger.get(f"/api/v1/submissions/{pending['id']}").status_code == 200
        assert any(item["id"] == pending["id"] for item in stranger.get("/api/v1/submissions?status=PENDING").json()["data"]["items"])
        assert viewer.get("/api/v1/submissions").status_code == 403
        assert mtbf_seconds(engine, users["admin"], payload["campaign_id"]) == 0
        with engine.connect() as conn:
            campaign = conn.execute(text("SELECT id FROM test.test_campaign WHERE public_id=:id"), {"id": payload["campaign_id"]}).scalar_one()
            assert conn.execute(text("SELECT count(*) FROM test.test_execution WHERE campaign_id=:id"), {"id": campaign}).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM test.runtime_interval WHERE campaign_id=:id"), {"id": campaign}).scalar_one() == 0
        csrf = admin.headers.pop("X-CSRF-Token")
        assert review(admin, pending).status_code == 403
        admin.headers["X-CSRF-Token"] = csrf
        reject_key = uuid4().hex
        rejection = review(admin, pending, "REJECTED", reject_key)
        assert rejection.status_code == 200, rejection.text
        assert review(admin, pending, "REJECTED", reject_key).status_code == 200
        assert review(admin, pending).status_code == 409
        assert mtbf_seconds(engine, users["admin"], payload["campaign_id"]) == 0
        assert submit(author, payload).status_code == 409
        assert submit(author, {**payload, "active_seconds": 45}, revision=1).status_code == 422
        response = submit(author, payload, revision=1)
        assert response.status_code == 201, response.text
        pending = response.json()["data"]
        assert pending["revision"] == 2 and len(pending["revisions"]) == 2
        approve_key = uuid4().hex
        approved = review(admin, pending, key=approve_key)
        assert approved.status_code == 200, approved.text
        formal = approved.json()["data"]
        assert formal["execution_id"] and formal["execution_code"]
        assert mtbf_seconds(engine, users["admin"], payload["campaign_id"]) == 0
        assert review(admin, pending, key=approve_key).status_code == 200
        assert submit(author, payload, revision=2).status_code == 409
        assert viewer.get(f"/api/v1/executions/{formal['execution_id']}").status_code == 200
        assert viewer.get(f"/api/v1/executions/{formal['execution_id']}").json()["data"]["result"]["completed_cycles"] == 3
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM test.test_execution WHERE campaign_id=:id"), {"id": campaign}).scalar_one() == 1
            assert float(conn.execute(text("SELECT active_seconds FROM test.runtime_interval WHERE campaign_id=:id"), {"id": campaign}).scalar_one()) == 50
            assert conn.execute(text("SELECT count(*) FROM reliability.exposure_assessment WHERE campaign_id=:id"), {"id": campaign}).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM integration.data_submission_review r JOIN integration.data_submission s ON s.id=r.submission_id WHERE s.public_id=:id"), {"id": pending["id"]}).scalar_one() == 2
            assert conn.execute(text("SELECT count(*) FROM audit.change_log WHERE table_name='data_submission_review' AND actor_user_id IS NOT NULL")).scalar_one() >= 2


def test_self_review_concurrency_and_publish_failure_roll_back_everything(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as author:
        payload = context(admin)
        own = submit(admin, payload).json()["data"]
        reviewed = review(admin, own)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["data"]["execution_id"]
        grant(admin, engine, users["first"], payload["campaign_id"])
        from datetime import datetime, timedelta
        later = {**payload, "execution_uuid": str(uuid4()),
                 "started_at": (datetime.fromisoformat(payload["started_at"])+timedelta(seconds=120)).isoformat(),
                 "ended_at": (datetime.fromisoformat(payload["ended_at"])+timedelta(seconds=120)).isoformat()}
        first = submit(author, later).json()["data"]
        # Independent cookie sessions race against the same row and revision.
        def decide():
            with signed_in(users["admin"]) as reviewer:
                return review(reviewer, first).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(lambda _: decide(), range(2))) == [200, 409]
        overlapping = submit(author, {**later, "execution_uuid": str(uuid4())}).json()["data"]
        assert review(admin, overlapping).status_code == 409
        current = admin.get(f"/api/v1/submissions/{overlapping['id']}").json()["data"]
        assert current["status"] == "PENDING" and current["execution_id"] is None
        assert current["revisions"][-1]["decision"] is None
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM test.test_execution WHERE approved_submission_id=(SELECT id FROM integration.data_submission WHERE public_id=:id)"), {"id": overlapping["id"]}).scalar_one() == 0


def test_old_ingestion_and_direct_database_write_cannot_bypass_approval(environment):
    _, users = environment
    with signed_in(users["admin"]) as admin:
        for suffix in ("executions", f"executions/{uuid4()}/finish", "executions/by-producer/old-run/telemetry"):
            r = admin.post("/api/v1/ingestion/" + suffix, json={})
            assert r.status_code == 404, r.text
    app_engine = create_engine(os.environ["DATABASE_URL"])
    with app_engine.connect() as conn:
        for table in ("test.test_execution", "test.runtime_interval", "test.execution_result", "health.metric_series", "integration.data_submission", "integration.data_submission_review"):
            assert conn.execute(text("SELECT has_table_privilege(current_user,:table,'INSERT,UPDATE,DELETE')"), {"table": table}).scalar_one() is False
    app_engine.dispose()


def test_context_changes_and_revoked_permissions_fail_closed(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as author:
        payload = context(admin)
        grant(admin, engine, users["first"], payload["campaign_id"])
        assert submit(author, {**payload, "station_id": str(uuid4())}).status_code == 422
        pending = submit(author, payload).json()["data"]
        with engine.begin() as conn:
            conn.execute(text("UPDATE iam.user_campaign_access SET access_level='VIEW' WHERE user_id=(SELECT id FROM iam.app_user WHERE username=:name)"), {"name": users["first"]})
        assert submit(author, {**payload, "execution_uuid": str(uuid4())}).status_code == 201
        with engine.begin() as conn:
            conn.execute(text("UPDATE test.station SET enabled=false WHERE public_id=:id"), {"id": payload["station_id"]})
        assert review(admin, pending).status_code == 422
        assert review(admin, pending, "REJECTED").status_code == 200


def test_approved_blocked_summary_is_ended_and_never_a_live_execution(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as author:
        payload = {**context(admin), "source_status": "blocked"}
        grant(admin, engine, users["first"], payload["campaign_id"])
        pending = submit(author, payload).json()["data"]
        r = review(admin, pending)
        assert r.status_code == 200, r.text
        asset_detail = admin.get(f"/api/v1/assets/{payload['asset_id']}")
        assert asset_detail.status_code == 200, asset_detail.text
        assert asset_detail.json()["data"]["current_context"]["execution_id"] is None
