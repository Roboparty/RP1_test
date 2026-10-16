from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from sqlalchemy.exc import DBAPIError
from starlette.concurrency import run_in_threadpool

from app.core.auth import RequestActor, require_roles
from app.core.config import get_settings
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.archives import ArchiveRepository
from app.schemas.archives import ArchiveAnalysisCreate, ArchiveClassification, ArchiveCreate, ArchiveSubmit, ArchiveVerification
from app.schemas.common import DataEnvelope
from app.services.archive_storage import ArchiveStorage
from app.services.curve_query import CurveQueryService


router = APIRouter(prefix="/api/v1/archives", tags=["central-test-archives"])
Operator = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR"))]
Reader = Annotated[RequestActor, Depends(require_roles("SYSTEM_ADMIN", "TEST_EXECUTOR", "VIEWER"))]
Key = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]
Reference = Annotated[str, Query(min_length=1,max_length=300)]


def operation(call):
    try:
        return DataEnvelope(data=call())
    except DBAPIError as exc:
        code = getattr(exc.orig,"sqlstate","") or ""
        if code=="42501":
            raise ApiError(403,"archive_forbidden","当前账号没有该归档的操作权限。") from exc
        if code=="P0002":
            raise ApiError(404,"archive_not_found","归档不存在或不可见。") from exc
        if code in {"P0001","23505","23P01","40P01","40001"}:
            raise ApiError(409,"archive_conflict","归档版本、上传状态或幂等键冲突，请刷新核对；本次变更已回滚。") from exc
        if code.startswith("22") or code in {"23514","23503","23502"}:
            raise ApiError(422,"invalid_archive","采集事实、核实信息或正式执行上下文不符合要求。") from exc
        raise


@router.get("")
def listing(_: Reader, session: DbSession, execution_uuid: UUID | None=None, execution_id: UUID | None=None,
            q: Annotated[str | None,Query(max_length=200)]=None,
            origin: Literal["HMI","HISTORY","OTHER"] | None=None,
            classification_state: Literal["UNCLASSIFIED","CLASSIFIED"] | None=None,
            processing_state: Literal["WAITING","QUEUED","PROCESSING","READY","FAILED"] | None=None,
            before: Annotated[int | None,Query(ge=1)]=None,limit: Annotated[int,Query(ge=1,le=100)]=50):
    return DataEnvelope(data=ArchiveRepository(session).list(execution_uuid,execution_id,q,origin,classification_state,processing_state,before,limit))


@router.get("/{reference}")
def detail(reference: str, _: Reader, session: DbSession):
    return DataEnvelope(data=ArchiveRepository(session).get(reference,history=True))


@router.post("",status_code=201)
def create(payload: ArchiveCreate, _: Operator, session: DbSession, idempotency_key: Key):
    if payload.size_bytes>get_settings().archive_max_file_bytes:
        raise ApiError(413,"archive_too_large","文件超过中央归档允许的大小。")
    return operation(lambda: ArchiveRepository(session).create(payload,idempotency_key))


@router.put("/{reference}/content")
async def content(reference: str,request: Request, _: Operator,session: DbSession,
                  offset: Annotated[int,Query(ge=0)],idempotency_key: Key):
    settings = get_settings()
    if request.headers.get("content-type","").split(";",1)[0].strip().lower()!="application/octet-stream":
        raise ApiError(415,"invalid_content_type","原始上传块必须使用 application/octet-stream。")
    body = bytearray()
    async for chunk in request.stream():
        if len(body)+len(chunk)>settings.archive_max_chunk_bytes:
            raise ApiError(413,"archive_chunk_too_large","上传块超过允许大小，请按块继续上传。")
        body.extend(chunk)
    def append():
        repository = ArchiveRepository(session)
        initial = repository.get(reference)
        storage = ArchiveStorage(settings.archive_root)
        with storage.lock(initial["execution_uuid"]):
            archive = repository.lock_upload(initial["id"])
            received = storage.append(archive["execution_uuid"],archive["received_bytes"],archive["size_bytes"],offset,bytes(body),archive["upload_state"]=="COMPLETE")
            return repository.update_upload(archive["id"],received,"COMPLETE" if archive["upload_state"]=="COMPLETE" else "UPLOADING")
    return await run_in_threadpool(operation,append)


@router.post("/{reference}/complete",status_code=202)
def complete(reference: str, _: Operator,session: DbSession,idempotency_key: Key):
    def queue():
        repository = ArchiveRepository(session)
        archive = repository.lock_upload(reference)
        if archive["received_bytes"]!=archive["size_bytes"]:
            raise ApiError(409,"incomplete_upload","原始文件尚未完整接收，继续上传后再完成归档。")
        # SHA256 validation and parsing happen in the recoverable archive worker.
        return repository.update_upload(archive["id"],archive["size_bytes"],"COMPLETE")
    return operation(queue)


@router.post("/{reference}/verification")
def verify(reference: str,payload: ArchiveVerification, _: Operator,session: DbSession,idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).edit(reference,"verification",payload,idempotency_key))


@router.post("/{reference}/classification")
def classify(reference: str,payload: ArchiveClassification, _: Operator,session: DbSession,idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).edit(reference,"classification",payload,idempotency_key))


@router.post("/{reference}/submit")
def submit(reference: str,payload: ArchiveSubmit, _: Operator,session: DbSession,idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).submit(reference,payload,idempotency_key))


@router.get("/{reference}/analyses")
def analyses(reference: str, _: Reader,session: DbSession):
    return DataEnvelope(data=ArchiveRepository(session).analyses(reference))


@router.post("/{reference}/analyses",status_code=201)
def analyze(reference: str,payload: ArchiveAnalysisCreate, _: Operator,session: DbSession,idempotency_key: Key):
    return operation(lambda: ArchiveRepository(session).add_analysis(reference,payload,idempotency_key))


@router.get("/{reference}/curves")
def curves(reference: str, _: Reader,session: DbSession,
           metrics: Annotated[str,Query(max_length=4000)]="",subjects: Annotated[str,Query(max_length=4000)]="",
           start: float | None=None,end: float | None=None,
           max_points: Annotated[int,Query(ge=50,le=20000)]=4000,
           stage: Annotated[str | None,Query(max_length=100)]=None,
           cycle: Annotated[str | None,Query(max_length=100)]=None):
    archive = ArchiveRepository(session).get(reference)
    settings = get_settings()
    query = CurveQueryService(ArchiveStorage(settings.archive_root),settings.archive_query_memory_mb)
    return DataEnvelope(data=query.query(archive,[m.strip() for m in metrics.split(",") if m.strip()],
        [s.strip() for s in subjects.split(",") if s.strip()],start,end,max_points,stage,cycle))
