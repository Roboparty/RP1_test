import os
import re
from contextlib import contextmanager
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.main import app


pytestmark = [pytest.mark.integration, pytest.mark.skipif(not os.environ.get("HMI_TEST_ADMIN_URL"), reason="requires isolated HMI_TEST_ADMIN_URL")]
PASSWORD = "Isolated-Presence-Test-2026!"


@pytest.fixture
def environment():
    url = os.environ["HMI_TEST_ADMIN_URL"]
    if not url.rsplit("/", 1)[-1].startswith("rp1_presence"):
        pytest.fail("HMI integration tests require a dedicated rp1_presence database")
    engine = create_engine(url)
    users = {}
    password_hash = PasswordHasher().hash(PASSWORD)
    with engine.begin() as connection:
        for role, key in (("SYSTEM_ADMIN", "admin"), ("TEST_EXECUTOR", "first"), ("TEST_EXECUTOR", "second"), ("VIEWER", "viewer")):
            username = f"presence-{key}-{uuid4().hex[:12]}"
            user_id = connection.execute(text("INSERT INTO iam.app_user(username,display_name,role,principal_kind,must_change_password) VALUES (:name,:name,:role,'HUMAN',false) RETURNING id"), {"name": username, "role": role}).scalar_one()
            connection.execute(text("INSERT INTO iam.user_credential(user_id,password_hash) VALUES (:id,:hash)"), {"id": user_id, "hash": password_hash})
            users[key] = username
    previous = app.dependency_overrides.copy()
    app.dependency_overrides.clear()
    yield engine, users
    app.dependency_overrides.clear()
    app.dependency_overrides.update(previous)
    engine.dispose()


@contextmanager
def signed_in(username):
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        response = client.post("/api/v1/auth/login", json={"username": username, "password": PASSWORD})
        assert response.status_code == 200, response.text
        client.headers["X-CSRF-Token"] = client.cookies["rp1_csrf"]
        yield client


def heartbeat_payload(installation=None):
    return {
        "installation_id": installation or str(uuid4()), "station_name": "Synthetic QA workstation",
        "bench_id": "QA-01", "sequence": 1, "snapshot_age_seconds": 0,
        "runs": [{"session_key": "left", "state": "running", "test_id": "QA-ONLY", "sample_id": "SYNTHETIC", "active_seconds": 123}],
        "plc": {"driver": "mock", "phase": "RUNNING", "stale": False},
    }


def find_station(client, installation):
    response = client.get("/api/v1/hmi/stations?limit=200")
    assert response.status_code == 200, response.text
    return next((item for item in response.json()["data"]["items"] if item["installation_id"] == installation), None)


def test_real_cookie_login_presence_visibility_lifecycle_and_no_test_writes(environment):
    engine, users = environment
    connection_id = str(uuid4())
    payload = heartbeat_payload()
    route = "/api/v1/hmi/presence/" + connection_id
    with engine.connect() as database:
        original_executions = database.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one()
    with signed_in(users["first"]) as operator, signed_in(users["second"]) as stranger, signed_in(users["admin"]) as admin:
        sent = operator.put(route, json=payload)
        assert sent.status_code == 200, sent.text
        assert find_station(admin, payload["installation_id"])["state"] == "running"
        assert find_station(operator, payload["installation_id"])["operator_name"] == users["first"]
        assert find_station(stranger, payload["installation_id"]) is None
        assert stranger.put(route, json={**payload, "sequence": 2}).status_code == 409
        assert stranger.delete(route).status_code == 404
        assert operator.put(route, json=payload).status_code == 200
        assert operator.put(route, json={**payload, "sequence": 2, "runs": []}).status_code == 200
        assert find_station(admin, payload["installation_id"])["state"] == "powered"
        assert operator.put(route, json=payload).status_code == 200
        assert find_station(admin, payload["installation_id"])["state"] == "powered"
        with engine.begin() as database:
            database.execute(text("UPDATE integration.hmi_presence SET last_seen_at = now() - interval '46 seconds' WHERE connection_id=:id"), {"id": connection_id})
        assert find_station(admin, payload["installation_id"])["state"] == "offline"
        assert operator.put(route, json={**payload, "sequence": 3}).status_code == 200
        assert find_station(admin, payload["installation_id"])["state"] == "running"
        assert operator.delete(route).status_code == 200
        assert find_station(admin, payload["installation_id"])["state"] == "offline"
        assert operator.put(route, json={**payload, "sequence": 4}).status_code == 409
    with engine.connect() as database:
        assert database.execute(text("SELECT count(*) FROM test.test_execution")).scalar_one() == original_executions


def test_authentication_csrf_viewer_and_disabled_account_fail_closed(environment):
    engine, users = environment
    payload = heartbeat_payload()
    route = "/api/v1/hmi/presence/" + str(uuid4())
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/v1/hmi/stations").status_code == 401
    with signed_in(users["viewer"]) as viewer:
        assert viewer.put(route, json=payload).status_code == 403
    with signed_in(users["first"]) as operator, signed_in(users["admin"]) as admin:
        csrf = operator.headers.pop("X-CSRF-Token")
        assert operator.put(route, json=payload).status_code == 403
        operator.headers["X-CSRF-Token"] = csrf
        assert operator.put(route, json=payload).status_code == 200
        with engine.begin() as database:
            database.execute(text("UPDATE iam.app_user SET enabled=false WHERE username=:username"), {"username": users["first"]})
        assert operator.put(route, json={**payload, "sequence": 2}).status_code == 401
        assert find_station(admin, payload["installation_id"])["online"] is False


def test_new_login_supersedes_old_station_session_and_logout_is_immediate(environment):
    _, users = environment
    installation = str(uuid4())
    with signed_in(users["first"]) as first, signed_in(users["second"]) as second, signed_in(users["admin"]) as admin:
        old_route = "/api/v1/hmi/presence/" + str(uuid4())
        new_route = "/api/v1/hmi/presence/" + str(uuid4())
        assert first.put(old_route, json=heartbeat_payload(installation)).status_code == 200
        assert second.put(new_route, json=heartbeat_payload(installation)).status_code == 200
        assert first.put(old_route, json={**heartbeat_payload(installation), "sequence": 2}).status_code == 200
        assert find_station(admin, installation)["operator_name"] == users["second"]
        assert find_station(first, installation) is None
        assert second.post("/api/v1/auth/logout").status_code == 204
        assert find_station(admin, installation)["state"] == "offline"


def test_only_a_complete_running_heartbeat_opens_one_official_execution(environment):
    engine, users = environment
    suffix = uuid4().hex[:8]
    with signed_in(users["admin"]) as admin, signed_in(users["first"]) as operator:
        def create(path, payload):
            response = admin.post("/api/v1/setup/" + path, json={**payload, "reason": "live running regression"})
            assert response.status_code == 201, response.text
            return response.json()["data"]

        catalog = admin.get("/api/v1/setup/catalog")
        assert catalog.status_code == 200, catalog.text
        case = next(item for item in catalog.json()["data"] if item["target_part_code"] == "LEG" and item["enabled"] and item["version_id"])
        sample = create("assets", {
            "asset_code": f"RP1.3-SLEG-{int(suffix, 16) % 900 + 100:03d}",
            "asset_kind": "MODULE", "target_part_code": "SLEG", "product_family": "QA",
            "serial_number": suffix, "display_name": "Live sample", "model": "QA",
            "configuration_fingerprint": "QA-" + suffix,
        })
        program = create("programs", {"program_code": "QA-LIVE-" + suffix, "name": "Live program"})
        campaign = create("campaigns", {
            "program_id": program["id"], "campaign_code": "QA-LIVE-PLAN-" + suffix,
            "name": "Live plan", "asset_kind": "MODULE",
        })
        station = create("stations", {
            "site_code": "QA-SITE-" + suffix, "site_name": "QA", "lab_code": "QA-LAB-" + suffix,
            "lab_name": "QA", "station_code": "QA-" + suffix[:4].upper(), "name": "QA station",
            "station_type": "MODULE",
        })
        created = create(f"campaigns/{campaign['id']}/contexts", {
            "asset_id": sample["id"], "configuration_id": sample["configuration_id"],
            "test_case_version_id": case["version_id"], "station_id": station["id"],
            "cycle_code": "QA-LIVE-" + suffix,
        })
        context = created["context"]
        payload = heartbeat_payload()
        payload["runs"][0].update({
            "state": "running", "active_seconds": 40,
            "campaign_id": context["campaign_id"], "cycle_id": context["cycle_id"],
            "asset_id": context["asset_id"], "configuration_id": context["configuration_id"],
            "test_case_version_id": context["test_case_version_id"], "station_id": context["station_id"],
        })
        route = "/api/v1/hmi/presence/" + str(uuid4())
        opened = operator.put(route, json=payload)
        assert opened.status_code == 200, opened.text
        live = opened.json()["data"]["executions"]
        assert len(live) == 1
        assert re.fullmatch(r".+_E[0-9]+", live[0]["execution_code"])
        assert live[0]["status"] == "RUNNING"
        repeated = operator.put(route, json={**payload, "sequence": 2, "runs": [{**payload["runs"][0], "active_seconds": 55}]})
        assert repeated.status_code == 200, repeated.text
        assert repeated.json()["data"]["executions"][0]["execution_id"] == live[0]["execution_id"]
        home = operator.get("/api/v1/dashboard/home")
        assert home.status_code == 200, home.text
        scopes = [item for group in home.json()["data"]["groups"] for item in group["objects"]]
        assert sum(item["active_execution_count"] for item in scopes) == 1
        stopped = operator.put(route, json={
            **payload, "sequence": 3,
            "runs": [{**payload["runs"][0], "state": "completed", "active_seconds": 55}],
        })
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["data"]["executions"][0]["status"] == "COMPLETED"
    with engine.connect() as database:
        row = database.execute(text("""
            SELECT e.status, e.reported_active_seconds, ri.active_seconds
            FROM test.test_execution e
            LEFT JOIN test.runtime_interval ri ON ri.execution_id = e.id AND NOT ri.voided
            WHERE e.public_id = :id
        """), {"id": live[0]["execution_id"]}).one()
        assert row.status == "COMPLETED"
        assert float(row.reported_active_seconds) == 55
        assert float(row.active_seconds) == 55
