from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Path, Query

from app.core.db import DbSession
from app.repositories.read_models import ReadModelRepository
from app.schemas.common import DataEnvelope
from app.schemas.read_models import (
    AssetDetail,
    AssetList,
    AssetPerformanceTrends,
    DataSourceHealthView,
    EventList,
    ExecutionDetail,
    ExecutionJointAnalysis,
    ExecutionList,
    MtbfConclusionList,
    TestCaseDetail,
    TestCaseDurationList,
)


router = APIRouter(prefix="/api/v1", tags=["read-models"])
AssetKindFilter = Annotated[Literal["WHOLE_MACHINE", "MODULE"] | None, Query()]
TargetPartFilter = Annotated[
    Literal["SARM", "SLEG", "SYS", "UPPER", "LOWER", "CHEST", "HEAD", "BAT"] | None,
    Query(),
]
PageLimit = Annotated[int, Query(ge=1, le=100)]
Cursor = Annotated[str | None, Query(max_length=500)]
Identifier = Annotated[str, Path(min_length=1, max_length=200)]


@router.get("/test-cases/durations", response_model=DataEnvelope[TestCaseDurationList])
def test_case_durations(
    session: DbSession,
    asset_kind: AssetKindFilter = None,
    asset: Annotated[str | None, Query(max_length=200)] = None,
    test_case: Annotated[str | None, Query(max_length=200)] = None,
    status: Annotated[str | None, Query(max_length=40)] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    limit: PageLimit = 50,
    cursor: Cursor = None,
) -> DataEnvelope[TestCaseDurationList]:
    return DataEnvelope(
        data=ReadModelRepository(session).test_case_durations(
            asset_kind=asset_kind,
            asset=asset,
            test_case=test_case,
            status=status,
            search=search,
            limit=limit,
            cursor=cursor,
        )
    )


@router.get("/test-cases/{identifier}", response_model=DataEnvelope[TestCaseDetail])
def test_case_detail(
    identifier: Identifier,
    session: DbSession,
    limit: PageLimit = 50,
    cursor: Cursor = None,
) -> DataEnvelope[TestCaseDetail]:
    return DataEnvelope(
        data=ReadModelRepository(session).get_test_case(
            identifier, limit=limit, cursor=cursor
        )
    )


@router.get("/executions/{identifier}", response_model=DataEnvelope[ExecutionDetail])
def execution_detail(
    identifier: Identifier,
    session: DbSession,
) -> DataEnvelope[ExecutionDetail]:
    return DataEnvelope(data=ReadModelRepository(session).get_execution(identifier))


@router.get(
    "/executions/{identifier}/joint-analysis",
    response_model=DataEnvelope[ExecutionJointAnalysis],
)
def execution_joint_analysis(
    identifier: Identifier,
    session: DbSession,
    subject_code: Annotated[
        str | None, Query(min_length=1, max_length=100)
    ] = None,
    stage_code: Annotated[
        str | None, Query(min_length=1, max_length=100)
    ] = None,
    cycle_index: Annotated[
        int | None, Query(ge=0, le=2_147_483_647)
    ] = None,
) -> DataEnvelope[ExecutionJointAnalysis]:
    return DataEnvelope(
        data=ReadModelRepository(session).get_execution_joint_analysis(
            identifier,
            subject_code=subject_code,
            stage_code=stage_code,
            cycle_index=cycle_index,
        )
    )


@router.get("/assets", response_model=DataEnvelope[AssetList])
def assets(
    session: DbSession,
    asset_kind: AssetKindFilter = None,
    target_part_code: TargetPartFilter = None,
    status: Annotated[str | None, Query(max_length=40)] = None,
    search: Annotated[str | None, Query(max_length=200)] = None,
    limit: PageLimit = 50,
    cursor: Cursor = None,
) -> DataEnvelope[AssetList]:
    return DataEnvelope(
        data=ReadModelRepository(session).list_assets(
            asset_kind=asset_kind,
            target_part_code=target_part_code,
            status=status,
            search=search,
            limit=limit,
            cursor=cursor,
        )
    )


@router.get("/assets/{identifier}", response_model=DataEnvelope[AssetDetail])
def asset_detail(
    identifier: Identifier,
    session: DbSession,
) -> DataEnvelope[AssetDetail]:
    return DataEnvelope(data=ReadModelRepository(session).get_asset(identifier))


@router.get(
    "/assets/{identifier}/performance-trends",
    response_model=DataEnvelope[AssetPerformanceTrends],
)
def asset_performance_trends(
    identifier: Identifier,
    session: DbSession,
) -> DataEnvelope[AssetPerformanceTrends]:
    return DataEnvelope(
        data=ReadModelRepository(session).asset_performance_trends(identifier)
    )


@router.get("/assets/{identifier}/executions", response_model=DataEnvelope[ExecutionList])
def asset_executions(
    identifier: Identifier,
    session: DbSession,
    limit: PageLimit = 50,
    cursor: Cursor = None,
) -> DataEnvelope[ExecutionList]:
    return DataEnvelope(
        data=ReadModelRepository(session).asset_executions(identifier, limit, cursor)
    )


@router.get("/assets/{identifier}/events", response_model=DataEnvelope[EventList])
def asset_events(
    identifier: Identifier,
    session: DbSession,
    limit: PageLimit = 50,
    cursor: Cursor = None,
) -> DataEnvelope[EventList]:
    return DataEnvelope(
        data=ReadModelRepository(session).asset_events(identifier, limit, cursor)
    )


@router.get("/mtbf/conclusions", response_model=DataEnvelope[MtbfConclusionList])
def mtbf_conclusions(
    session: DbSession,
    asset_kind: AssetKindFilter = None,
    limit: PageLimit = 50,
    cursor: Cursor = None,
) -> DataEnvelope[MtbfConclusionList]:
    return DataEnvelope(
        data=ReadModelRepository(session).mtbf_conclusions(
            asset_kind=asset_kind,
            limit=limit,
            cursor=cursor,
        )
    )


@router.get("/data-sources/health", response_model=DataEnvelope[DataSourceHealthView])
def data_source_health(session: DbSession) -> DataEnvelope[DataSourceHealthView]:
    return DataEnvelope(data=ReadModelRepository(session).data_sources_health())
