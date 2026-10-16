from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from sqlalchemy.exc import DBAPIError

from app.core.auth import RequestActor, require_roles
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.setup import SetupRepository
from app.schemas.common import DataEnvelope
from app.schemas.setup import AssetCreate, CampaignCreate, Change, ContextCreate, ProgramCreate, StationCreate

router = APIRouter(prefix="/api/v1/setup", tags=["setup"])
Admin = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN"))]
Editor = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR"))]


def write(operation):
    try:
        return DataEnvelope(data=operation())
    except DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", None)
        if code == "23505":
            raise ApiError(409, "duplicate", "编号、序列号或关联已存在，请检查后重试。") from exc
        if code in {"23514", "23503", "P0001", "22001"}:
            raise ApiError(422, "invalid_setup", "字段或关联不符合业务规则。") from exc
        if code == "42501":
            raise ApiError(403, "forbidden", "当前账户没有此对象的操作权限。") from exc
        raise


@router.get("/catalog", response_model=DataEnvelope[list[dict[str, Any]]])
def catalog(session: DbSession):
    return DataEnvelope(data=SetupRepository(session).catalog())


@router.get("/options", response_model=DataEnvelope[dict[str, Any]])
def options(session: DbSession):
    return DataEnvelope(data=SetupRepository(session).options())


@router.get("/campaigns", response_model=DataEnvelope[list[dict[str, Any]]])
def campaigns(session: DbSession):
    return DataEnvelope(data=SetupRepository(session).campaigns())


@router.get("/campaigns/{campaign_id}/contexts", response_model=DataEnvelope[list[dict[str, Any]]])
def contexts(campaign_id: UUID, session: DbSession):
    return DataEnvelope(data=SetupRepository(session).contexts(campaign_id))


@router.post("/assets", status_code=201, response_model=DataEnvelope[dict[str, Any]])
def asset(payload: AssetCreate, _: Admin, session: DbSession):
    return write(lambda: SetupRepository(session).create_asset(payload))


@router.post("/programs", status_code=201, response_model=DataEnvelope[dict[str, Any]])
def program(payload: ProgramCreate, _: Admin, session: DbSession):
    return write(lambda: SetupRepository(session).create_program(payload))


@router.post("/campaigns", status_code=201, response_model=DataEnvelope[dict[str, Any]])
def campaign(payload: CampaignCreate, _: Admin, session: DbSession):
    return write(lambda: SetupRepository(session).create_campaign(payload))


@router.post("/campaigns/{campaign_id}/activate", response_model=DataEnvelope[dict[str, Any]])
def activate(campaign_id: UUID, payload: Change, _: Admin, session: DbSession):
    return write(lambda: SetupRepository(session).activate_campaign(campaign_id, payload.reason))


@router.post("/stations", status_code=201, response_model=DataEnvelope[dict[str, Any]])
def station(payload: StationCreate, _: Admin, session: DbSession):
    return write(lambda: SetupRepository(session).create_station(payload))


@router.post("/campaigns/{campaign_id}/contexts", status_code=201, response_model=DataEnvelope[dict[str, Any]])
def configure(campaign_id: UUID, payload: ContextCreate, _: Editor, session: DbSession):
    return write(lambda: SetupRepository(session).create_context(campaign_id, payload))
