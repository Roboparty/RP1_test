"""Real cookies, RLS, immutable evidence and publication in disposable storage."""
import hashlib
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine,text

from app.core.config import get_settings
from app.schemas.archives import ArchiveCreate
from app.services.archive_storage import ArchiveStorage
from app.services.archive_worker import run_once
from test_archives import archive_payload
from test_hmi_presence_integration import environment,signed_in
from test_submissions_integration import context,grant,review


pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"),reason="isolated archive database required")]
RAW=b"time_s,joint_cmd_pos_rad,joint_pos_rad,joint_temp_c,joint_error_id\n0,0,0,20,0\n1,1,1,21,0\n2,0,0,20,0\n"


def headers(key=None):
    return {"Idempotency-Key": key or uuid4().hex}


def create(client,payload,key=None):
    response=client.post("/api/v1/archives",json=payload,headers=headers(key))
    assert response.status_code==201,response.text
    return response.json()["data"]


def upload(client,item,raw=RAW):
    response=client.put(f"/api/v1/archives/{item['id']}/content?offset=0",content=raw,headers={**headers(),"Content-Type":"application/octet-stream"})
    assert response.status_code==200,response.text
    response=client.post(f"/api/v1/archives/{item['id']}/complete",headers=headers())
    assert response.status_code==202,response.text
    for _ in range(30):
        if not run_once():
            break
        item=client.get(f"/api/v1/archives/{item['id']}").json()["data"]
        if item["processing_state"] in {"READY","FAILED"}:
            return item
    return client.get(f"/api/v1/archives/{item['id']}").json()["data"]


def verify(client,item,station_id,verdict="passed",key=None):
    return client.post(f"/api/v1/archives/{item['id']}/verification",json={"expected_revision":item["revision"],"tester":"Synthetic actual tester",
        "station_id":station_id,"verdict":verdict,"notes":"Synthetic operator notes","discrepancy_reason":"Synthetic explicit review of unconfigured criteria","reason":"Synthetic verification"},headers=headers(key))


def classify(client,item,ctx):
    return client.post(f"/api/v1/archives/{item['id']}/classification",json={"expected_revision":item["revision"],"context":{k:ctx[k] for k in ("campaign_id","cycle_id","segment_id","asset_id","configuration_id","test_case_version_id","station_id")},"reason":"Synthetic delayed classification"},headers=headers())


def submit(client,item,expected=0,key=None):
    return client.post(f"/api/v1/archives/{item['id']}/submit",json={"expected_revision":item["revision"],"expected_submission_revision":expected,"reason":"Synthetic archive submission"},headers=headers(key))


def test_other_archive_is_viewable_before_classification_and_only_approved_for_viewer(environment):
    engine,users=environment
    with signed_in(users["admin"]) as admin,signed_in(users["first"]) as author,signed_in(users["second"]) as stranger,signed_in(users["viewer"]) as viewer:
        ctx=context(admin)
        payload=archive_payload(origin="OTHER",raw=RAW)
        key=uuid4().hex
        item=create(author,payload,key)
        assert create(author,payload,key)["id"]==item["id"]
        assert create(author,payload)["id"]==item["id"]
        assert stranger.post("/api/v1/archives",json=payload,headers=headers()).status_code==403
        assert viewer.get(f"/api/v1/archives/{item['id']}").status_code==404
        assert stranger.get(f"/api/v1/archives/{item['id']}").json()["data"]["can_edit"] is False
        assert author.get(f"/api/v1/archives/{item['id']}/curves?metrics=pos_rad").status_code==409
        assert submit(author,item).status_code in {409,422}
        item=upload(author,item)
        assert item["processing_state"]=="READY",item
        assert item["manifest"]["original_sha256"]==payload["sha256"]
        assert item["manifest"]["analysis"]["verdict"]=="unconfigured"
        response=author.get(f"/api/v1/archives/{item['archive_code']}/curves?metrics=pos_rad&max_points=50")
        assert response.status_code==200,response.text
        assert response.json()["data"]["series"][0]["points"]==[[0.0,0.0],[1.0,1.0],[2.0,0.0]]
        verification=verify(author,item,ctx["station_id"])
        assert verification.status_code==200,verification.text
        item=verification.json()["data"]
        assert item["current_verification"]["station_code"].startswith("AP-")
        assert submit(author,item).status_code in {409,422}
        response=classify(author,item,ctx)
        assert response.status_code==200,response.text
        item=response.json()["data"]
        assert item["classification_state"]=="CLASSIFIED"
        submitted=submit(author,item)
        assert submitted.status_code==200,submitted.text
        item=submitted.json()["data"]
        assert item["submission_status"]=="PENDING"
        assert viewer.get(f"/api/v1/archives/{item['id']}").status_code==404
        approved=review(author,{"id":item["submission_id"],"revision":item["submission_revision"]})
        assert approved.status_code==200,approved.text
        grant(admin,engine,users["viewer"],ctx["campaign_id"],"VIEW")
        visible=viewer.get(f"/api/v1/archives/{item['id']}")
        assert visible.status_code==200,visible.text
        item=visible.json()["data"]
        assert item["submission_status"]=="APPROVED" and item["can_edit"] is False
        assert viewer.get(f"/api/v1/archives/{item['execution_id']}/curves?metrics=pos_rad").status_code==200
        linked=viewer.get("/api/v1/archives",params={"execution_id":item["execution_id"]})
        assert linked.json()["data"]["items"][0]["id"]==item["id"]
        assert verify(author,item,ctx["station_id"]).status_code==409
        assert ArchiveCreate.model_validate(payload).facts.model_dump(mode="json")==item["facts"]
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM integration.archive_submission WHERE archive_id=(SELECT id FROM integration.execution_archive WHERE public_id=:id)"),{"id":item["id"]}).scalar_one()==1


def test_raw_transfer_offset_retries_checksum_failure_recovery_and_csrf(environment):
    _,users=environment
    with signed_in(users["admin"]) as author:
        item=create(author,archive_payload(origin="OTHER",raw=RAW))
        base=f"/api/v1/archives/{item['id']}"
        first=RAW[:20]
        request_headers={**headers(),"Content-Type":"application/octet-stream"}
        csrf=author.headers.pop("X-CSRF-Token")
        assert author.put(base+"/content?offset=0",content=first,headers=request_headers).status_code==403
        author.headers["X-CSRF-Token"]=csrf
        sent=author.put(base+"/content?offset=0",content=first,headers=request_headers)
        assert sent.status_code==200,sent.text
        assert author.put(base+"/content?offset=0",content=first,headers=request_headers).json()["data"]["received_bytes"]==20
        assert author.put(base+"/content?offset=0",content=b"x"*20,headers=request_headers).status_code==409
        assert author.put(base+"/content?offset=21",content=b"x",headers=request_headers).status_code==409
        assert author.post(base+"/complete",headers=headers()).status_code==409
        # Corrupt only the second transferred chunk, retaining immutable expected SHA.
        bad=bytearray(RAW[20:]);bad[-3]=ord("9")
        assert author.put(base+"/content?offset=20",content=bytes(bad),headers=request_headers).status_code==200
        assert author.post(base+"/complete",headers=headers()).status_code==202
        assert run_once()
        failed=author.get(base).json()["data"]
        assert failed["upload_state"]=="CORRUPT" and failed["received_bytes"]==0
        assert failed["processing_error"]=="CHECKSUM_MISMATCH"
        recovered=upload(author,failed,RAW)
        assert recovered["processing_state"]=="READY",recovered
        raw_path=ArchiveStorage(get_settings().archive_root).path(item["execution_uuid"],"raw.csv")
        assert hashlib.sha256(raw_path.read_bytes()).hexdigest()==item["sha256"]
        assert author.put(base+"/content?offset=0",content=b"x"*20,headers=request_headers).status_code==409


def test_rejected_revision_can_change_review_but_never_measurements_or_raw(environment):
    _,users=environment
    with signed_in(users["admin"]) as author:
        ctx=context(author)
        initial={k:ctx[k] for k in ("campaign_id","cycle_id","segment_id","asset_id","configuration_id","test_case_version_id","station_id")}
        payload=archive_payload(context=initial,raw=RAW)
        item=upload(author,create(author,payload))
        item=verify(author,item,ctx["station_id"]).json()["data"]
        item=submit(author,item).json()["data"]
        rejection=review(author,{"id":item["submission_id"],"revision":item["submission_revision"]},"REJECTED")
        assert rejection.status_code==200,rejection.text
        item=author.get(f"/api/v1/archives/{item['id']}").json()["data"]
        changed=verify(author,item,ctx["station_id"],"blocked")
        assert changed.status_code==200,changed.text
        revised=changed.json()["data"]
        assert len(revised["verification_history"])==2 and revised["facts"]==ArchiveCreate.model_validate(payload).facts.model_dump(mode="json")
        assert submit(author,revised,expected=1).status_code==200
        mutable={**payload,"facts":{**payload["facts"],"active_seconds":1}}
        assert author.post("/api/v1/archives",json=mutable,headers=headers()).status_code==409
        assert author.post(f"/api/v1/archives/{item['id']}/verification",json={"expected_revision":revised["revision"],"tester":"QA","station_id":ctx["station_id"],"verdict":"blocked","notes":"QA","reason":"QA","active_seconds":1},headers=headers()).status_code==422


def test_database_role_cannot_overwrite_evidence_or_historical_versions(environment):
    app_engine=create_engine(os.environ["DATABASE_URL"])
    with app_engine.connect() as conn:
        for table in ("integration.execution_archive","integration.archive_verification","integration.archive_classification","integration.archive_submission"):
            assert conn.execute(text("SELECT has_table_privilege(current_user,:table,'INSERT,UPDATE,DELETE')"),{"table":table}).scalar_one() is False
    app_engine.dispose()
