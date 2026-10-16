from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import DBAPIError

from app.core.auth import RequestActor, RequestActorDep
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.hmi_presence import HmiPresenceRepository
from app.schemas.common import DataEnvelope
from app.schemas.hmi_presence import HmiHeartbeat, HmiStationList


router = APIRouter(prefix="/api/v1/hmi", tags=["hmi-presence"])


def require_hmi_operator(actor: RequestActorDep) -> RequestActor:
    if actor.principal_kind != "HUMAN" or actor.role not in {"TEST_EXECUTOR", "SYSTEM_ADMIN"} or actor.session_public_id is None:
        raise ApiError(403, "hmi_operator_required", "上位机需要测试执行员或管理员的有效登录会话。")
    return actor


HmiOperator = Annotated[RequestActor, Depends(require_hmi_operator)]


def mutate_presence(operation):
    try:
        return DataEnvelope(data=operation())
    except DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", "") or ""
        if code == "42501":
            raise ApiError(403, "acquisition_owner_required", "执行身份属于其他操作账号，不能接管该记录。") from exc
        if code in {"23505", "23P01", "P0001", "40P01", "40001"}:
            raise ApiError(409, "presence_execution_conflict", "执行身份或并发状态发生冲突，请核对后重试；本次心跳已回滚。") from exc
        if code.startswith("22"):
            raise ApiError(422, "invalid_presence_context", "执行身份与计划上下文不一致。") from exc
        raise


@router.put("/presence/{connection_id}")
def heartbeat(connection_id: UUID, payload: HmiHeartbeat, actor: HmiOperator, session: DbSession) -> DataEnvelope[dict]:
    return mutate_presence(lambda: HmiPresenceRepository(session).heartbeat(connection_id, payload, actor))


@router.delete("/presence/{connection_id}")
def disconnect(connection_id: UUID, actor: HmiOperator, session: DbSession) -> DataEnvelope[dict]:
    return mutate_presence(lambda: HmiPresenceRepository(session).disconnect(connection_id, actor))


@router.get("/stations")
def stations(
    actor: RequestActorDep,
    session: DbSession,
    cursor: UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> DataEnvelope[HmiStationList]:
    if actor.principal_kind != "HUMAN":
        raise ApiError(403, "forbidden", "工位监控需要登录账号。")
    return DataEnvelope(data=HmiPresenceRepository(session).list_stations(cursor=cursor, limit=limit))
