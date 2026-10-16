"""Supported historical CSV intake; immutable bytes use the existing archive API."""
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.routes.archives import Key, Operator, operation
from app.core.config import get_settings
from app.core.db import DbSession
from app.core.errors import ApiError
from app.repositories.archives import ArchiveRepository
from app.schemas.archives import ArchiveImportCreate
from app.schemas.common import DataEnvelope

router = APIRouter(prefix="/api/v1/archive-imports", tags=["historical-csv-import"])


@router.get("/lookup")
def lookup(_: Operator, session: DbSession,
          sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
          size_bytes: Annotated[int, Query(ge=1, le=50 * 1024**3)]):
    return DataEnvelope(data=ArchiveRepository(session).import_match(sha256, size_bytes))


@router.post("", status_code=201)
def initialize(payload: ArchiveImportCreate, _: Operator, session: DbSession, idempotency_key: Key):
    if payload.size_bytes > get_settings().archive_max_file_bytes:
        raise ApiError(413, "archive_too_large", "文件超过中央归档允许的大小。")
    return operation(lambda: ArchiveRepository(session).import_create(payload, idempotency_key))
