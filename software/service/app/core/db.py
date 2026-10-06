from __future__ import annotations

from collections.abc import Generator
from typing import Annotated

from fastapi import Depends
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.auth import RequestActorDep
from app.core.config import get_settings


settings = get_settings()
engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


def get_db(
    actor: RequestActorDep,
) -> Generator[Session, None, None]:
    with SessionLocal() as session:
        with session.begin():
            session.execute(
                text("SELECT iam.set_request_context(:user_id, :request_id, :reason)"),
                {
                    "user_id": str(actor.public_id),
                    "request_id": actor.request_id,
                    "reason": actor.change_reason,
                },
            )
            session.execute(
                text(
                    "SELECT set_config("
                    "'app.source_ip', coalesce(:source_ip, ''), true)"
                ),
                {"source_ip": actor.source_ip},
            )
            yield session


def get_public_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        try:
            yield session
        finally:
            session.rollback()


DbSession = Annotated[Session, Depends(get_db)]
PublicDbSession = Annotated[Session, Depends(get_public_db)]
