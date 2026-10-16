"""Restartable central archive worker; no device access and no approval authority."""
import json
import logging
import time
from uuid import uuid4

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.archive_storage import ArchiveStorage
from app.services.curve_query import CurveProcessor


logger = logging.getLogger("rp1.archive_worker")


def set_context(session, request_id):
    session.execute(text("SELECT iam.set_request_context(:user,:request,:reason)"),
        {"user": get_settings().worker_user_public_id,"request": request_id,"reason": "中央 CSV 校验与可重建曲线分析"})


def run_once() -> bool:
    """Claim and finish one synthetic/real queued archive; convenient for isolated QA."""
    settings = get_settings()
    token = uuid4()
    request_id = f"archive-worker:{token}"
    with SessionLocal.begin() as session:
        set_context(session,request_id)
        row = session.execute(text("SELECT * FROM integration.archive_claim(:token)"), {"token": token}).mappings().first()
        archive = dict(row) if row else None
    if archive is None:
        return False
    last_heartbeat = 0.0
    def progress(manifest=None,error=None,force=False):
        nonlocal last_heartbeat
        if not force and manifest is None and error is None and time.monotonic()-last_heartbeat<20:
            return
        with SessionLocal.begin() as session:
            set_context(session,request_id)
            alive = session.execute(text("SELECT integration.archive_progress(:id,:token,CAST(:manifest AS jsonb),:error)"),
                {"id": archive["public_id"],"token": token,"manifest": json.dumps(manifest,ensure_ascii=False,allow_nan=False) if manifest is not None else None,"error": error}).scalar_one()
        if not alive:
            raise RuntimeError("归档处理租约已被恢复任务替换")
        last_heartbeat = time.monotonic()
    try:
        storage = ArchiveStorage(settings.archive_root)
        with storage.lock(archive["execution_uuid"],processing=True), storage.lock(archive["execution_uuid"]):
            progress(force=True)
            valid = storage.finalize(archive["execution_uuid"],archive["size_bytes"],archive["sha256"],progress)
            if not valid:
                progress(error="CHECKSUM_MISMATCH",force=True)
                return True
        # No database transaction spans file hashing or CSV conversion.
        with storage.lock(archive["execution_uuid"],processing=True):
            manifest = CurveProcessor(storage,settings.archive_query_memory_mb).build(archive,progress)
            progress(manifest=manifest,force=True)
    except Exception as exc:
        logger.exception("archive_processing_failed",extra={"execution_uuid": str(archive["execution_uuid"])})
        try:
            error = str(exc) if isinstance(exc,ValueError) else "中央文件处理失败，请核对存储并重试归档处理。"
            progress(error=error[:1000],force=True)
        except Exception:
            logger.exception("archive_failure_receipt_failed")
    return True


def run_worker():
    while True:
        try:
            worked = run_once()
        except Exception:
            logger.exception("archive_worker_iteration_failed")
            worked = False
        if not worked:
            time.sleep(get_settings().archive_worker_poll_seconds)


if __name__=="__main__":
    logging.basicConfig(level=logging.INFO)
    run_worker()
