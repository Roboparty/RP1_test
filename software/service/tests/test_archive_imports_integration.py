"""Real HTTP/SQL intake, digest concurrency and publication boundaries; isolated DB only."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.core.config import get_settings
from app.schemas.archives import ArchiveCreate
from app.services.archive_storage import ArchiveStorage
from test_archive_imports import import_payload
from test_archives import archive_payload
from test_archives_integration import create, headers, upload, verify, submit
from test_hmi_presence_integration import environment, signed_in
from test_submissions_integration import context, review

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated CSV import database required")]


def sample_bytes():
    return f"time_s,j{uuid4().hex}_pos_rad\n0,0\n1,9\n2,-4\n".encode()


def intake(client, payload, key=None):
    response = client.post("/api/v1/archive-imports", json=payload, headers=headers(key))
    assert response.status_code == 201, response.text
    return response.json()["data"]


def test_import_resume_digest_duplicate_permissions_and_unknown_facts(environment):
    engine, users = environment
    raw = sample_bytes()
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as author, signed_in(users["second"]) as other, signed_in(users["viewer"]) as viewer:
        ctx = context(admin)
        payload = import_payload(ctx["station_id"], raw)
        lookup = {k: payload[k] for k in ("sha256", "size_bytes")}
        assert author.get("/api/v1/archive-imports/lookup", params=lookup).json()["data"] == {"archive": None}
        key = uuid4().hex
        first = intake(author, payload, key)
        item = first["archive"]
        assert not first["reused"] and item["origin"] == "HISTORY"
        assert item["classification_state"] == "UNCLASSIFIED" and item["current_verification"] is None
        assert item["station_label"].startswith("AP-")
        assert item["facts"]["active_seconds"] is None and item["facts"]["completed_cycles"] is None
        assert item["facts"]["exception_count"] is None and item["facts"]["machine_status"] == "unknown"
        assert intake(author, payload, key)["archive"]["execution_uuid"] == item["execution_uuid"]
        assert author.post("/api/v1/archive-imports", json={**payload, "test_name": "different"}, headers=headers(key)).status_code == 409
        first_chunk = raw[:20]
        put = author.put(f"/api/v1/archives/{item['id']}/content?offset=0", content=first_chunk,
                         headers={**headers(), "Content-Type": "application/octet-stream"})
        assert put.status_code == 200, put.text
        retry = intake(author, {**payload, "filename": "renamed.csv", "test_name": "do not replace"})
        assert retry["reused"] and retry["archive"]["received_bytes"] == 20
        assert retry["archive"]["filename"] == payload["filename"] and retry["archive"]["test_name"] == payload["test_name"]
        # Resume at the acknowledged offset; chunk retry cannot change its bounds.
        resumed = author.put(f"/api/v1/archives/{item['id']}/content?offset=20", content=raw[20:],
                             headers={**headers(), "Content-Type": "application/octet-stream"})
        assert resumed.status_code == 200, resumed.text
        item = upload(author, item, raw)
        assert item["processing_state"] == "READY", item
        assert item["manifest"]["analysis"]["verdict"] == "blocked"
        assert item["manifest"]["row_count"] == 3
        assert ArchiveStorage(get_settings().archive_root).path(item["execution_uuid"], "raw.csv").read_bytes() == raw
        assert author.get(f"/api/v1/archives/{item['execution_uuid']}/curves?metrics=pos_rad").status_code == 200
        check = verify(author, item, ctx["station_id"], "blocked")
        assert check.status_code == 200, check.text
        item = check.json()["data"]
        assert item["current_verification"]["tester"] != item["owner_name"]
        blocked = submit(author, item)
        assert blocked.status_code == 422 and blocked.json()["error"]["code"] == "history_acquisition_facts_missing"
        assert viewer.get("/api/v1/archive-imports/lookup", params=lookup).status_code == 403
        assert viewer.post("/api/v1/archive-imports", json=payload, headers=headers()).status_code == 403
        visible = other.get("/api/v1/archive-imports/lookup", params=lookup).json()["data"]["archive"]
        assert visible["id"] == item["id"] and visible["can_edit"] is False
        assert other.put(f"/api/v1/archives/{item['id']}/content?offset=0", content=raw,
                         headers={**headers(), "Content-Type": "application/octet-stream"}).status_code == 403
        reused = intake(author, payload)
        assert reused["archive"]["revision"] == item["revision"] and reused["archive"]["current_verification"] == item["current_verification"]
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM integration.execution_archive WHERE sha256=:sha"), {"sha": payload["sha256"]}).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM integration.data_submission WHERE execution_uuid=:uuid"), {"uuid": item["execution_uuid"]}).scalar_one() == 0
            assert conn.execute(text("SELECT count(*) FROM test.test_execution WHERE acquisition_uuid=:uuid"), {"uuid": item["execution_uuid"]}).scalar_one() == 0


def test_import_csrf_invalid_station_and_client_fact_injection(environment):
    _, users = environment
    with signed_in(users["admin"]) as admin:
        ctx = context(admin)
        payload = import_payload(ctx["station_id"], sample_bytes())
        csrf = admin.headers.pop("X-CSRF-Token")
        assert admin.post("/api/v1/archive-imports", json=payload, headers=headers()).status_code == 403
        admin.headers["X-CSRF-Token"] = csrf
        for key in ("facts", "active_seconds", "execution_uuid", "acceptance"):
            assert admin.post("/api/v1/archive-imports", json={**payload, key: {}}, headers=headers()).status_code == 422
        assert admin.post("/api/v1/archive-imports", json={**payload, "station_id": str(uuid4())}, headers=headers()).status_code == 422
        assert admin.get("/api/v1/archive-imports/lookup", params={"sha256": "bad", "size_bytes": 1}).status_code == 422


def test_same_csv_as_approved_hmi_record_reuses_frozen_execution(environment):
    _, users = environment
    raw = sample_bytes()
    with signed_in(users["admin"]) as author:
        ctx = context(author)
        formal = {k: ctx[k] for k in ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")}
        item = upload(author, create(author, archive_payload(context=formal, raw=raw)), raw)
        checked = verify(author, item, ctx["station_id"])
        assert checked.status_code == 200, checked.text
        submitted = submit(author, checked.json()["data"])
        assert submitted.status_code == 200, submitted.text
        pending = submitted.json()["data"]
        approved = review(author, {"id": pending["submission_id"], "revision": pending["submission_revision"]})
        assert approved.status_code == 200, approved.text
        original = author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        reused = intake(author, import_payload(ctx["station_id"], raw))
        assert reused["reused"] and reused["archive"]["id"] == original["id"]
        assert reused["archive"]["can_edit"] is False and reused["archive"]["submission_status"] == "APPROVED"
        for key in ("origin", "execution_uuid", "execution_id", "facts", "revision", "classification", "current_verification", "test_name", "filename"):
            assert reused["archive"][key] == original[key]


def test_concurrent_imports_share_one_identity_and_database_gate_blocks_fabrication(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin:
        ctx = context(admin)
    payload = import_payload(ctx["station_id"], sample_bytes())
    payload["classification"] = {k: ctx[k] for k in ("campaign_id", "cycle_id", "segment_id", "asset_id", "configuration_id", "test_case_version_id", "station_id")}
    with engine.connect() as conn:
        actor = conn.execute(text("SELECT public_id FROM iam.app_user WHERE username=:name"), {"name": users["admin"]}).scalar_one()
    app_engine = create_engine(os.environ["DATABASE_URL"])
    barrier = Barrier(2)
    def initialize(_):
        with app_engine.begin() as conn:
            conn.execute(text("SELECT iam.set_request_context(:actor,:request,'isolated import concurrency')"), {"actor": actor, "request": uuid4().hex})
            barrier.wait(timeout=10)
            return dict(conn.execute(text("SELECT * FROM integration.archive_import(CAST(:payload AS jsonb),:key)"),
                {"payload": json.dumps(payload), "key": uuid4().hex}).mappings().one())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(initialize, range(2)))
    assert results[0]["archive_id"] == results[1]["archive_id"] and sorted(r["reused"] for r in results) == [False, True]
    def rejected(sql, p):
        with pytest.raises(DBAPIError) as error:
            with app_engine.begin() as conn:
                conn.execute(text("SELECT iam.set_request_context(:actor,:request,'isolated malicious client')"), {"actor": actor, "request": uuid4().hex})
                conn.execute(text(sql), p)
        assert error.value.orig.sqlstate == "22023"
    rejected("SELECT * FROM integration.archive_import(CAST(:payload AS jsonb),:key)",
             {"payload": json.dumps({**payload, "facts": {"active_seconds": 0}}), "key": uuid4().hex})
    with engine.connect() as conn:
        internal_id = conn.execute(text("SELECT id FROM integration.execution_archive WHERE public_id=:id"), {"id": results[0]["archive_id"]}).scalar_one()
    rejected("SELECT integration.archive_summary(:id)", {"id": internal_id})
    legacy = ArchiveCreate.model_validate(archive_payload(origin="HISTORY")).model_dump(mode="json")
    legacy["facts"]["active_seconds"] = None
    rejected("SELECT integration.archive_create(CAST(:payload AS jsonb),:key)", {"payload": json.dumps(legacy), "key": uuid4().hex})
    app_engine.dispose()
