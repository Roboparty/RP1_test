from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.core.db import DbSession
from app.repositories.mtbf import MtbfRepository
from app.repositories.read_models import ReadModelRepository
from app.schemas.common import DataEnvelope
from app.schemas.dashboard import DashboardOverview
from app.schemas.read_models import DashboardHome


router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])
AssetKindFilter = Annotated[Literal["WHOLE_MACHINE", "MODULE"] | None, Query()]


@router.get("/home", response_model=DataEnvelope[DashboardHome])
def home(session: DbSession) -> DataEnvelope[DashboardHome]:
    return DataEnvelope(data=ReadModelRepository(session).dashboard_home())


@router.get("/overview", response_model=DataEnvelope[DashboardOverview])
def overview(
    session: DbSession,
    asset_kind: AssetKindFilter = None,
) -> DataEnvelope[DashboardOverview]:
    return DataEnvelope(data=MtbfRepository(session).dashboard_overview(asset_kind))
