from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy.exc import DBAPIError

from app.core.auth import RequestActor, require_roles
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.submissions import SubmissionRepository
from app.schemas.common import DataEnvelope
from app.schemas.submissions import SubmissionCreate, SubmissionReview

router = APIRouter(prefix="/api/v1/submissions", tags=["test-data-approval"])
Operator = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR"))]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


def write(operation):
    try:
        return DataEnvelope(data=operation())
    except DBAPIError as exc:
        code = getattr(exc.orig, "sqlstate", "") or ""
        if code == "42501":
            raise ApiError(403, "forbidden", "权限不足或提交人账号已停用。") from exc
        if code == "P0002":
            raise ApiError(404, "not_found", "提交不存在。") from exc
        if code == "23P01":
            raise ApiError(409, "submission_conflict", "该样品在同一时段已有运行记录，不能再记一条重叠时长；这次发布已回滚。") from exc
        if code in {"P0001", "23505", "40P01", "40001"}:
            raise ApiError(409, "submission_conflict", "版本或幂等键发生冲突，请刷新核对；失败的发布已回滚。") from exc
        if code.startswith("22") or code in {"23514", "23503", "23502"}:
            raise ApiError(422, "invalid_submission", "摘要或执行上下文不符合要求，可能已停用。") from exc
        raise


@router.get("")
def submissions(_: Operator, session: DbSession, status: Literal["PENDING", "APPROVED", "REJECTED"] | None = None,
                before: Annotated[int | None, Query(ge=1)] = None, limit: Annotated[int, Query(ge=1, le=100)] = 50):
    return DataEnvelope(data=SubmissionRepository(session).list(status, before, limit))


@router.get("/{submission_id}")
def detail(submission_id: UUID, _: Operator, session: DbSession):
    return DataEnvelope(data=SubmissionRepository(session).get(submission_id))


@router.post("", status_code=201)
def submit(payload: SubmissionCreate, _: Operator, session: DbSession, idempotency_key: Key):
    return write(lambda: SubmissionRepository(session).submit(payload, idempotency_key))


@router.post("/{submission_id}/review")
def review(submission_id: UUID, payload: SubmissionReview, _: Operator, session: DbSession, idempotency_key: Key):
    return write(lambda: SubmissionRepository(session).review(submission_id, payload, idempotency_key))
