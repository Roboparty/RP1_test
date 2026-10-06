from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from app.core.db import engine


router = APIRouter(tags=["system"])


@router.get("/healthz")
def liveness() -> dict:
    return {"status": "ok"}


@router.get("/readyz")
def readiness() -> dict:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return {"status": "ready"}
