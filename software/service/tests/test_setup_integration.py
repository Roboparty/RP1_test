"""HTTP + RLS regression against a disposable, empty migrated database."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import text

from test_hmi_presence_integration import environment, signed_in

pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="isolated database required")]


def post(client, path, payload, expected=201):
    response = client.post("/api/v1/setup/" + path, json={**payload, "reason": "isolated setup regression"})
    assert response.status_code == expected, response.text
    return response.json().get("data")


def data(client, path):
    response = client.get("/api/v1/" + path)
    assert response.status_code == 200, response.text
    return response.json()["data"]


def asset(code="RP1.3-SLEG-001", serial=None):
    return dict(asset_code=code,asset_kind="MODULE",target_part_code="SLEG",product_family="QA",serial_number=serial or uuid4().hex,display_name="Isolated sample",model="QA model",configuration_fingerprint="QA-config")


def test_empty_catalog_home_and_complete_setup_with_permission_isolation(environment):
    engine, users = environment
    with signed_in(users["admin"]) as admin, signed_in(users["viewer"]) as viewer, signed_in(users["first"]) as editor, signed_in(users["second"]) as stranger:
        catalog = data(viewer, "setup/catalog")
        assert len(catalog) == 68
        home = data(admin, "dashboard/home")
        scopes = [item for group in home["groups"] for item in group["objects"]]
        assert len(scopes) == 10
        assert all(s["part_asset_count"] == s["active_execution_count"] == 0 for s in scopes)
        assert sum(len(s["test_cases"]) for s in scopes) == 67
        assert all(c["execution_count"] == 0 and c["effective_exposure_seconds"] == 0 for s in scopes for c in s["test_cases"])
        assert data(viewer, "setup/campaigns") == []
        post(viewer, "assets", asset(), 403)
        post(editor, "assets", asset(), 403)
        sample = post(admin, "assets", asset())
        post(admin, "assets", asset(), 409)
        post(admin, "assets", {**asset("RP1.3-SLEG-002"), "target_part_code": "SARM"}, 422)
        program = post(admin, "programs", dict(program_code="QA-PROGRAM",name="QA Program"))
        campaign = post(admin, "campaigns", dict(program_id=program["id"],campaign_code="QA-PLAN",name="QA Plan",asset_kind="MODULE"))
        post(admin, "campaigns", dict(program_id=program["id"],campaign_code="QA-PLAN",name="Duplicate",asset_kind="MODULE"), 409)
        post(admin, "campaigns", dict(program_id=program["id"],campaign_code="QA-DATES",name="Dates",asset_kind="MODULE",planned_start="2026-10-10T00:00:00Z",planned_end="2026-10-09T00:00:00Z"), 422)
        # New plans are readable by every enabled human non-admin; EDIT stays explicit.
        created = data(viewer, "setup/campaigns")
        assert [(p["id"], p["can_edit"]) for p in created] == [(campaign["id"], False)]
        assert created[0]["stages"] == [{"sequence_no": 1, "stage_name": "测试阶段1"}]
        assert [(p["id"], p["can_edit"]) for p in data(editor, "setup/campaigns")] == [(campaign["id"], False)]
        with engine.connect() as conn:
            ids = {row.username: str(row.public_id) for row in conn.execute(text("SELECT username,public_id FROM iam.app_user"))}
        revoked = admin.request("DELETE", f"/api/v1/admin/users/{ids[users['second']]}/campaign-grants/{campaign['id']}", json={"reason": "isolated regression"})
        assert revoked.status_code == 204, revoked.text
        assert [(p["id"], p["can_edit"]) for p in data(stranger, "setup/campaigns")] == [(campaign["id"], False)]
        station = post(admin, "stations", dict(site_code="QA-SITE",site_name="QA Site",lab_code="QA-LAB",lab_name="QA Lab",station_code="QA-01",name="QA station",station_type="MODULE"))
        case = next(c for c in catalog if c["target_part_code"] == "LEG" and c["enabled"] and c["version_id"])
        context_payload = dict(asset_id=sample["id"],configuration_id=sample["configuration_id"],test_case_version_id=case["version_id"],station_id=station["id"],cycle_code="QA-CYCLE")
        url = f"campaigns/{campaign['id']}/contexts"
        post(editor, url, context_payload, 404)
        configured = post(admin, url, context_payload)
        assert set(configured["context"]) == {"campaign_id","asset_id","configuration_id","test_case_version_id","station_id","cycle_id","segment_id"}
        post(admin, url, context_payload, 409)
        assert len(data(admin, "setup/" + url)) == 1
        assert len(data(stranger, "setup/" + url)) == 1
        assert len(data(stranger, "setup/options")["assets"]) == 1
        # Use the existing audited grant API; do not manufacture service credentials.
        post(editor, f"campaigns/{campaign['id']}/activate", {}, 403)
        post(admin, f"campaigns/{campaign['id']}/activate", {}, 200)
        post(admin, f"campaigns/{campaign['id']}/activate", {}, 409)
        grant = admin.post(f"/api/v1/admin/users/{ids[users['first']]}/campaign-grants", json={"campaign_id": campaign["id"], "access_level": "EDIT", "reason": "isolated regression"})
        assert grant.status_code == 200, grant.text
        # A second plan in the same project must remain visible to a VIEW grantee
        # even if the older project RLS hides its project metadata.
        second_plan = post(admin, "campaigns", dict(program_id=program["id"],campaign_code="QA-READ-PLAN",name="Read plan",asset_kind="MODULE"))
        post(admin, f"campaigns/{second_plan['id']}/activate", {}, 200)
        read_grant = admin.post(f"/api/v1/admin/users/{ids[users['viewer']]}/campaign-grants", json={"campaign_id": second_plan["id"], "access_level": "VIEW", "reason": "isolated regression"})
        assert read_grant.status_code == 200, read_grant.text
        assert [p["id"] for p in data(viewer,"setup/campaigns")] == [second_plan["id"], campaign["id"]]
        assert [p["id"] for p in data(stranger,"setup/campaigns")] == [second_plan["id"], campaign["id"]]
        assert data(viewer,"setup/options")["programs"] == []
        assert data(stranger,"setup/options")["programs"] == []
        assert [(p["id"], p["can_edit"]) for p in data(editor, "setup/campaigns")] == [(second_plan["id"], False), (campaign["id"], True)]
        assert len(data(editor, "setup/options")["assets"]) == 1
        post(editor, url, context_payload, 409)
        other_case = next(c for c in catalog if c["target_part_code"] == "LEG" and c["enabled"] and c["version_id"] != case["version_id"] and c["version_id"])
        post(editor, url, {**context_payload,"test_case_version_id":other_case["version_id"],"cycle_code":"QA-CYCLE-2"})
        incompatible = next(c for c in catalog if c["target_part_code"] == "ARM" and c["enabled"] and c["version_id"])
        post(editor, url, {**context_payload,"test_case_version_id":incompatible["version_id"],"cycle_code":"QA-MISMATCH"}, 422)
        # Unique cycle failure after membership insert must roll back the entire operation.
        third_case = next(c for c in catalog if c["target_part_code"] == "LEG" and c["enabled"] and c["version_id"] not in {case["version_id"],other_case["version_id"]} and c["version_id"])
        post(admin, url, {**context_payload,"test_case_version_id":third_case["version_id"]}, 409)
        assert len(data(admin,"setup/"+url)) == 2
        csrf = editor.headers.pop("X-CSRF-Token")
        post(editor, url, context_payload, 403)
        editor.headers["X-CSRF-Token"] = csrf
        with engine.connect() as conn:
            for table in ("test.test_execution","test.runtime_interval","reliability.exposure_assessment"):
                assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one() == 0
            audit = conn.execute(text("SELECT count(*) FROM audit.change_log WHERE change_reason='isolated setup regression' AND actor_user_id IS NOT NULL AND request_id<>''")).scalar_one()
            assert audit >= 12
            assert conn.execute(text("SELECT count(*) FROM test.test_cycle WHERE cycle_code='QA-MISMATCH'")).scalar_one() == 0
