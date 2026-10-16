"""Cross-route publication invariants with real cookies and disposable PostgreSQL."""
from concurrent.futures import ThreadPoolExecutor
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from test_archives import archive_payload
from test_archives_integration import RAW, classify, create, headers, submit, upload, verify
from test_hmi_presence_integration import environment, heartbeat_payload, signed_in
from test_setup_integration import post
from test_submissions_integration import context, review

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="disposable database required")]
CONTEXT_KEYS = ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")


def planned(ctx):
    return {key: ctx[key] for key in CONTEXT_KEYS}


def live_payload(ctx, execution_uuid):
    payload = heartbeat_payload()
    payload["runs"] = [{"session_key": "synthetic-acquisition", "state": "running", "execution_uuid": execution_uuid,
                        "active_seconds": 1, **{key: ctx[key] for key in CONTEXT_KEYS if key != "segment_id"}}]
    return payload


def test_old_summary_route_requires_archive_and_never_allows_measurement_replacement(environment):
    _, users = environment
    with signed_in(users["admin"]) as author:
        ctx = context(author)
        rejected = author.post("/api/v1/submissions", json={"summary": ctx, "reason": "No archive evidence"}, headers=headers())
        assert rejected.status_code == 422, rejected.text
        item = upload(author, create(author, archive_payload(context=planned(ctx), raw=RAW)))
        item = verify(author, item, ctx["station_id"], "blocked").json()["data"]
        sent = submit(author, item)
        assert sent.status_code == 200, sent.text
        item = sent.json()["data"]
        detail = author.get(f"/api/v1/submissions/{item['submission_id']}").json()["data"]
        frozen = detail["revisions"][-1]["summary"]
        assert frozen["source_status"] == "passed"
        assert review(author, {"id": item["submission_id"], "revision": item["submission_revision"]}, "REJECTED").status_code == 200
        for field, value in (("active_seconds", 1), ("completed_cycles", 9), ("exception_count", 9), ("source_status", "failed")):
            changed = author.post("/api/v1/submissions", json={"summary": {**frozen, field: value}, "expected_revision": 1, "reason": "Attempt to change acquisition facts"}, headers=headers())
            assert changed.status_code == 422, (field, changed.text)
        item = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        item = verify(author, item, ctx["station_id"], "blocked").json()["data"]
        item = submit(author, item, expected=1).json()["data"]
        approved = review(author, {"id": item["submission_id"], "revision": 2})
        assert approved.status_code == 200, approved.text
        result = author.get(f"/api/v1/executions/{approved.json()['data']['execution_id']}").json()["data"]["result"]
        assert result["source_status"] == "passed" and result["outcome"] == "INCONCLUSIVE"
        assert result["executor_display"] == "Synthetic actual tester"


def test_uuid_ownership_reconnect_station_correction_aliases_and_late_heartbeat(environment):
    engine, users = environment
    with signed_in(users["admin"]) as owner, signed_in(users["first"]) as stranger:
        ctx = context(owner)
        request = archive_payload(context=planned(ctx), raw=RAW)
        identity = request["execution_uuid"]
        payload = live_payload(ctx, identity)
        connection = str(uuid4())
        route = f"/api/v1/hmi/presence/{connection}"
        started = owner.put(route, json=payload)
        assert started.status_code == 200, started.text
        live = started.json()["data"]["executions"][0]
        assert stranger.post("/api/v1/archives", json=request, headers=headers()).status_code == 403
        stolen = stranger.put(f"/api/v1/hmi/presence/{uuid4()}", json={**payload, "installation_id": str(uuid4())})
        assert stolen.status_code == 403, stolen.text
        # Expiration closes the projection, but reconnecting the same acquisition
        # must reuse it rather than collide with its unique stable identity.
        with engine.begin() as database:
            database.execute(text("UPDATE integration.hmi_presence SET last_seen_at=clock_timestamp()-interval '46 seconds' WHERE connection_id=:id"), {"id": connection})
        owner.get("/api/v1/hmi/stations")
        resumed = owner.put(route, json={**payload, "sequence": 2})
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["data"]["executions"][0]["execution_id"] == live["execution_id"]
        tag = uuid4().hex[:8].upper()
        station = post(owner, "stations", {"site_code": "AC-"+tag, "site_name": "Actual QA site", "lab_code": "AC-"+tag,
            "lab_name": "Actual QA lab", "station_code": "AC-"+tag, "name": "Corrected actual station", "station_type": "MODULE"})
        item = upload(owner, create(owner, request))
        item = verify(owner, item, station["id"]).json()["data"]
        classified = classify(owner, item, {**ctx, "station_id": station["id"]})
        assert classified.status_code == 200, classified.text
        item = classified.json()["data"]
        assert len(item["classification_history"]) == 2
        item = submit(owner, item).json()["data"]
        approved = review(owner, {"id": item["submission_id"], "revision": item["submission_revision"]})
        assert approved.status_code == 200, approved.text
        item = owner.get(f"/api/v1/archives/{item['id']}").json()["data"]
        assert item["execution_id"] == live["execution_id"]
        assert item["execution_code"] != live["execution_code"]
        assert owner.get(f"/api/v1/archives/{live['execution_code']}").json()["data"]["id"] == item["id"]
        assert owner.get(f"/api/v1/executions/{live['execution_code']}").json()["data"]["execution"]["id"] == live["execution_id"]
        late = owner.put(route, json={**payload, "sequence": 3})
        assert late.status_code == 200, late.text
        assert late.json()["data"]["executions"][0]["status"] == "COMPLETED"
        with engine.connect() as database:
            assert database.execute(text("SELECT count(*) FROM test.test_execution WHERE acquisition_uuid=:id"), {"id": identity}).scalar_one() == 1
            assert database.execute(text("SELECT count(*) FROM test.runtime_interval WHERE execution_id=(SELECT id FROM test.test_execution WHERE acquisition_uuid=:id) AND NOT voided"), {"id": identity}).scalar_one() == 1


def test_review_and_duplicate_archive_submit_have_no_deadlock_or_half_publication(environment):
    engine, users = environment
    with signed_in(users["admin"]) as author:
        ctx = context(author)
        item = upload(author, create(author, archive_payload(context=planned(ctx), raw=RAW)))
        item = verify(author, item, ctx["station_id"]).json()["data"]
        item = submit(author, item).json()["data"]
        def approve():
            with signed_in(users["admin"]) as client:
                return review(client, {"id": item["submission_id"], "revision": item["submission_revision"]}).status_code
        def duplicate():
            with signed_in(users["admin"]) as client:
                return submit(client, item).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(approve), pool.submit(duplicate)
            assert first.result(timeout=20) == 200
            assert second.result(timeout=20) == 409
        with engine.connect() as database:
            assert database.execute(text("SELECT count(*) FROM test.test_execution WHERE approved_submission_id=(SELECT id FROM integration.data_submission WHERE public_id=:id)"), {"id": item["submission_id"]}).scalar_one() == 1


def test_historical_declared_time_is_versioned_and_never_replaces_raw_clock(environment):
    _, users = environment
    with signed_in(users["admin"]) as author:
        ctx = context(author)
        request = archive_payload(context=planned(ctx), origin="HISTORY", raw=RAW)
        dates = {"declared_started_at": request["facts"]["started_at"], "declared_ended_at": request["facts"]["ended_at"]}
        request["facts"].update(started_at=None, ended_at=None, clock_quality="MISSING")
        item = upload(author, create(author, request))
        check = {"expected_revision": item["revision"], "tester": "Historical QA tester", "station_id": ctx["station_id"], "verdict": "blocked",
                 "notes": "Original historical note", "discrepancy_reason": "Retrospective declaration", "reason": "Declared time, not a machine timestamp", **dates}
        changed = author.post(f"/api/v1/archives/{item['id']}/verification", json=check, headers=headers())
        assert changed.status_code == 200, changed.text
        item = submit(author, changed.json()["data"]).json()["data"]
        assert review(author, {"id": item["submission_id"], "revision": 1}, "REJECTED").status_code == 200
        item = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        revised = author.post(f"/api/v1/archives/{item['id']}/verification", json={**check, "expected_revision": item["revision"], "notes": "Corrected note, same declared dates"}, headers=headers())
        assert revised.status_code == 200, revised.text
        item = submit(author, revised.json()["data"], expected=1).json()["data"]
        detail = author.get(f"/api/v1/submissions/{item['submission_id']}").json()["data"]
        assert detail["revisions"][-1]["summary"]["clock_quality"] == "INVALID"
        assert item["facts"]["started_at"] is None and len(item["verification_history"]) == 2
