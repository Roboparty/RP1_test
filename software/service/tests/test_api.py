from datetime import datetime, timezone
from uuid import uuid4

from fastapi.testclient import TestClient

from app.core.auth import RequestActor, get_request_actor
from app.main import app


client = TestClient(app)
app.dependency_overrides[get_request_actor] = lambda: RequestActor(
    public_id=uuid4(),
    request_id="api-unit-test",
    change_reason="",
    source_ip="127.0.0.1",
    username="unit-viewer",
    display_name="Unit Viewer",
    role="VIEWER",
    principal_kind="HUMAN",
    session_public_id=uuid4(),
    session_type="BROWSER",
    expires_at=datetime.now(timezone.utc),
    must_change_password=False,
)


def test_health():
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_preview_is_explicitly_non_formal_and_has_no_pass_fail_gate():
    response = client.post(
        "/api/v1/calculations/preview",
        json={
            "exposure_hours": 1860,
            "relevant_failure_count": 2,
            "confidence_levels": [0.70, 0.90],
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["formal"] is False
    assert data["point_estimate_hours"] == 930
    assert "statistical_gate" not in data
    assert data["method"] == "mtbf.poisson.exposure_estimate.v1"


def test_inconsistent_preview_is_422_with_standard_error_envelope():
    response = client.post(
        "/api/v1/calculations/preview",
        json={"exposure_hours": 0, "relevant_failure_count": 1},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_p0_read_model_paths_are_in_openapi_contract():
    contract = client.get("/openapi.json").json()
    paths = contract["paths"]
    required = {
        "/api/v1/dashboard/home",
        "/api/v1/test-cases/durations",
        "/api/v1/test-cases/{identifier}",
        "/api/v1/executions/{identifier}",
        "/api/v1/executions/{identifier}/joint-analysis",
        "/api/v1/assets",
        "/api/v1/assets/{identifier}",
        "/api/v1/assets/{identifier}/performance-trends",
        "/api/v1/assets/{identifier}/executions",
        "/api/v1/assets/{identifier}/events",
        "/api/v1/mtbf/conclusions",
        "/api/v1/data-sources/health",
        "/api/v1/ingestion/executions",
        "/api/v1/ingestion/executions/{execution_id}/heartbeat",
        "/api/v1/ingestion/executions/{execution_id}/events",
        "/api/v1/ingestion/executions/{execution_id}/telemetry",
        "/api/v1/ingestion/executions/{execution_id}/artifacts",
        "/api/v1/ingestion/executions/{execution_id}/finish",
        "/api/v1/ingestion/executions/by-producer/{producer_execution_key}/heartbeat",
        "/api/v1/ingestion/executions/by-producer/{producer_execution_key}/events",
        "/api/v1/ingestion/executions/by-producer/{producer_execution_key}/telemetry",
        "/api/v1/ingestion/executions/by-producer/{producer_execution_key}/artifacts",
        "/api/v1/ingestion/executions/by-producer/{producer_execution_key}/finish",
        "/api/v1/admin/service-principals",
        "/api/v1/admin/service-principals/{service_id}/rotate",
        "/api/v1/admin/service-principals/{service_id}/revoke",
    }
    assert required <= set(paths)
    schemas = contract["components"]["schemas"]
    assert "as_of_at" in schemas["TestCaseDurationList"]["required"]
    assert {"asset_id", "metrics", "as_of_at"} <= set(
        schemas["AssetPerformanceTrends"]["required"]
    )
    assert {"date-time", None} == {
        branch.get("format") for branch in schemas["FreshnessView"]["properties"]["data_cutoff_at"]["anyOf"]
    }
    assert {
        "execution_id",
        "execution_code",
        "subjects",
        "stages",
        "cycles",
        "metrics",
        "resolved_selection",
        "summary",
        "series",
        "as_of_at",
        "data_cutoff_at",
    } <= set(schemas["ExecutionJointAnalysis"]["required"])


def test_ingestion_contract_requires_bearer_idempotency_and_bounded_batches():
    contract = client.get("/openapi.json").json()
    operation = contract["paths"][
        "/api/v1/ingestion/executions/{execution_id}/telemetry"
    ]["post"]
    assert operation["security"] == [{"ServiceBearer": []}]
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert parameters["Idempotency-Key"]["required"] is True
    schemas = contract["components"]["schemas"]
    assert schemas["TelemetryBatchRequest"]["properties"]["series"]["maxItems"] == 20
    assert schemas["TelemetrySeries"]["properties"]["points"]["maxItems"] == 5000
    assert schemas["TelemetrySeries"]["properties"]["sampling_interval_ms"][
        "minimum"
    ] == 10
    producer_operation = contract["paths"][
        "/api/v1/ingestion/executions/by-producer/"
        "{producer_execution_key}/telemetry"
    ]["post"]
    assert producer_operation["security"] == [{"ServiceBearer": []}]
    producer_parameters = {
        item["name"]: item for item in producer_operation["parameters"]
    }
    producer_key = producer_parameters["producer_execution_key"]["schema"]
    assert producer_key["minLength"] == 1
    assert producer_key["maxLength"] == 200
    assert producer_key["pattern"] == (
        "^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,199}$"
    )
    assert producer_parameters["Idempotency-Key"]["required"] is True


def test_joint_analysis_publishes_bounded_filter_parameters():
    operation = client.get("/openapi.json").json()["paths"][
        "/api/v1/executions/{identifier}/joint-analysis"
    ]["get"]
    parameters = {item["name"]: item["schema"] for item in operation["parameters"]}
    assert parameters["subject_code"]["anyOf"][0]["maxLength"] == 100
    assert parameters["stage_code"]["anyOf"][0]["maxLength"] == 100
    assert parameters["cycle_index"]["anyOf"][0]["minimum"] == 0
    assert parameters["cycle_index"]["anyOf"][0]["maximum"] == 2_147_483_647


def test_paginated_read_models_publish_limit_and_cursor_parameters():
    paths = client.get("/openapi.json").json()["paths"]
    for path in (
        "/api/v1/assets",
        "/api/v1/test-cases/durations",
        "/api/v1/test-cases/{identifier}",
        "/api/v1/mtbf/conclusions",
        "/api/v1/assets/{identifier}/executions",
        "/api/v1/assets/{identifier}/events",
    ):
        parameters = {item["name"]: item for item in paths[path]["get"]["parameters"]}
        assert parameters["limit"]["schema"]["maximum"] == 100
        assert parameters["limit"]["schema"]["minimum"] == 1
        assert "cursor" in parameters
    duration_parameters = {
        item["name"]: item
        for item in paths["/api/v1/test-cases/durations"]["get"]["parameters"]
    }
    assert "status" in duration_parameters
    asset_parameters = {
        item["name"]: item for item in paths["/api/v1/assets"]["get"]["parameters"]
    }
    assert "target_part_code" in asset_parameters
