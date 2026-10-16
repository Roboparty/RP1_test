"""Published-rule provenance, idempotency and approved-result immutability."""
import json
import os
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from test_archive_analysis import RULES
from test_archives import archive_payload
from test_archives_integration import create,headers,upload,verify,submit
from test_hmi_presence_integration import environment,signed_in
from test_submissions_integration import context,grant,review


pytestmark=[pytest.mark.integration,pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"),reason="isolated archive database required")]
PEAK_CSV=b"time_s,joint_pos_rad,joint_temp_c\n0,0,20\n1,0,120\n2,0,20\n"


def published_version(engine,ctx,rules=None,status="PUBLISHED"):
    with engine.begin() as conn:
        return str(conn.execute(text("""INSERT INTO catalog.test_case_version(test_case_id,version,procedure_spec,stage_definitions,decision_rules,sampling_requirements,status,published_at)
            SELECT test_case_id,:version,'{}'::jsonb,'[]'::jsonb,CAST(:rules AS jsonb),'{}'::jsonb,:status,clock_timestamp()
            FROM catalog.test_case_version WHERE public_id=:original RETURNING public_id"""),
            {"original":ctx["test_case_version_id"],"version":"SYNTHETIC-ANALYSIS-"+uuid4().hex,"rules":json.dumps(rules or {"pass_criteria":"Synthetic prose only"}),"status":status}).scalar_one())


def test_new_rule_analysis_is_append_only_idempotent_and_does_not_change_approved_result(environment):
    engine,users=environment
    with signed_in(users["admin"]) as admin,signed_in(users["first"]) as owner,signed_in(users["second"]) as stranger,signed_in(users["viewer"]) as viewer:
        ctx=context(admin)
        initial={key:ctx[key] for key in ("campaign_id","cycle_id","segment_id","asset_id","configuration_id","test_case_version_id","station_id")}
        item=upload(owner,create(owner,archive_payload(context=initial,raw=PEAK_CSV)),PEAK_CSV)
        item=verify(owner,item,ctx["station_id"]).json()["data"]
        item=submit(owner,item).json()["data"]
        approved=review(owner,{"id":item["submission_id"],"revision":item["submission_revision"]})
        assert approved.status_code==200,approved.text
        before=owner.get(f"/api/v1/archives/{item['id']}").json()["data"]
        assert before["submission_status"]=="APPROVED" and before["can_analyze"] is True and before["can_edit"] is False
        with engine.connect() as conn:
            result_before=conn.execute(text("SELECT to_jsonb(r) FROM test.execution_result r JOIN test.test_execution e ON e.id=r.execution_id WHERE e.public_id=:id"),{"id":before["execution_id"]}).scalar_one()
        version=published_version(engine,ctx,{"archive_acceptance":RULES})
        request={"test_case_version_id":version,"reason":"Synthetic retrospective thermal analysis"}
        key=uuid4().hex
        response=owner.post(f"/api/v1/archives/{item['id']}/analyses",json=request,headers=headers(key))
        assert response.status_code==201,response.text
        analysis=response.json()["data"]
        assert analysis["version"]==1 and analysis["mapping_status"]=="MAPPED" and analysis["report"]["verdict"]=="failed"
        assert analysis["source_test_case_version_id"]==version and analysis["rules_snapshot"]["decision_rules"]["archive_acceptance"]==RULES
        assert analysis["acceptance"]["criteria_version"]=="CATALOG:"+version
        assert owner.post(f"/api/v1/archives/{item['id']}/analyses",json=request,headers=headers(key)).json()["data"]["id"]==analysis["id"]
        assert owner.post(f"/api/v1/archives/{item['id']}/analyses",json={**request,"reason":"different request"},headers=headers(key)).status_code==409
        assert stranger.post(f"/api/v1/archives/{item['id']}/analyses",json=request,headers=headers()).status_code==403
        # Existing published-campaign policy grants authenticated viewers VIEW;
        # this approved archive follows that policy rather than adding a new gate.
        assert viewer.get(f"/api/v1/archives/{item['id']}/analyses").status_code==200
        grant(admin,engine,users["viewer"],ctx["campaign_id"],"VIEW")
        visible=viewer.get(f"/api/v1/archives/{item['id']}/analyses")
        assert visible.status_code==200 and len(visible.json()["data"]["items"])==1
        assert viewer.post(f"/api/v1/archives/{item['id']}/analyses",json=request,headers=headers()).status_code==403
        # Capture source rules, so later catalog edits do not rewrite this version.
        with engine.begin() as conn:
            conn.execute(text("UPDATE catalog.test_case_version SET decision_rules='{}'::jsonb WHERE public_id=:id"),{"id":version})
        assert owner.get(f"/api/v1/archives/{item['id']}/analyses").json()["data"]["items"][0]["rules_snapshot"]["decision_rules"]["archive_acceptance"]==RULES
        after=owner.get(f"/api/v1/archives/{item['id']}").json()["data"]
        assert after==before
        with engine.connect() as conn:
            result_after=conn.execute(text("SELECT to_jsonb(r) FROM test.execution_result r JOIN test.test_execution e ON e.id=r.execution_id WHERE e.public_id=:id"),{"id":before["execution_id"]}).scalar_one()
            assert result_after==result_before
            assert conn.execute(text("SELECT count(*) FROM integration.archive_analysis_version WHERE archive_id=(SELECT id FROM integration.execution_archive WHERE public_id=:id)"),{"id":item["id"]}).scalar_one()==1


def test_prose_only_draft_and_manual_rule_injection_are_not_formal_numeric_rules(environment):
    engine,users=environment
    with signed_in(users["admin"]) as owner:
        ctx=context(owner)
        item=upload(owner,create(owner,archive_payload(origin="OTHER",raw=PEAK_CSV)),PEAK_CSV)
        base=f"/api/v1/archives/{item['id']}/analyses"
        version=published_version(engine,ctx)
        response=owner.post(base,json={"test_case_version_id":version,"reason":"Synthetic prose source"},headers=headers())
        assert response.status_code==201,response.text
        analysis=response.json()["data"]
        assert analysis["mapping_status"]=="UNCONFIGURED" and analysis["report"]["verdict"]=="unconfigured"
        assert analysis["acceptance"]["max_temperature_rise_c"] is None
        draft=published_version(engine,ctx,{"archive_acceptance":RULES},status="DRAFT")
        assert owner.post(base,json={"test_case_version_id":draft,"reason":"Draft must fail"},headers=headers()).status_code==422
        assert owner.post(base,json={"test_case_version_id":version,"reason":"Rules injection must fail","acceptance":RULES},headers=headers()).status_code==422
        with engine.begin() as conn:
            # Even the application SQL entrypoint cannot relabel arbitrary manual
            # rules as published version thresholds.
            actor=conn.execute(text("SELECT public_id FROM iam.app_user WHERE username=:name"),{"name":users["admin"]}).scalar_one()
            conn.execute(text("SELECT iam.set_request_context(:id,'analysis-db-guard','Synthetic source guard')"),{"id":actor})
            with pytest.raises(DBAPIError) as rejected:
                conn.execute(text("SELECT integration.archive_add_analysis(:id,:source,CAST(:rules AS jsonb),'MAPPED','Synthetic invalid source',CAST(:report AS jsonb),'Synthetic manual injection',:key)"),
                    {"id":item["id"],"source":version,"rules":json.dumps({**analysis["acceptance"],"criteria_version":"CATALOG:"+version,"max_temperature_rise_c":999}),"report":json.dumps(analysis["report"]),"key":uuid4().hex})
            assert rejected.value.orig.sqlstate=="22023"
            conn.rollback()
