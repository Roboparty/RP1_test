from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel


T = TypeVar("T")


class DataEnvelope(BaseModel, Generic[T]):
    data: T


class ErrorDetail(BaseModel):
    field: str | None = None
    message: str
    code: str


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] = []


class ErrorEnvelope(BaseModel):
    error: ErrorBody
