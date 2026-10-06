from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.routes import (
    access,
    admin,
    auth,
    dashboard,
    health,
    ingestion,
    mtbf,
    read_models,
)
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import ApiError
from app.repositories.security import SecurityRepository
from app.services.auth import AuthService


settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("rp1.api")


@asynccontextmanager
async def lifespan(_: FastAPI):
    if settings.seed_admin_username and settings.seed_admin_password is not None:
        with SessionLocal() as session:
            bootstrapped = AuthService(
                settings, SecurityRepository(session)
            ).bootstrap_initial_admin()
        if bootstrapped:
            logger.info("initial administrator credential bootstrapped")
    yield


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "RP1整机与模块可靠性平台API。Campaign/scope实时MTBF由"
        "mtbf.poisson.exposure_estimate.v1确定性规则计算。"
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "If-Match",
        "X-Request-Id",
        "X-Change-Reason",
        "X-CSRF-Token",
        *(["X-User-Public-Id"] if settings.auth_mode == "development" else []),
    ],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(ApiError)
async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "code": exc.code,
                "message": exc.message,
                "details": exc.details,
            }
        },
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    details = [
        {
            "field": ".".join(str(item) for item in error["loc"] if item != "body"),
            "message": error["msg"],
            "code": error["type"],
        }
        for error in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "请求参数不符合接口契约。",
                "details": details,
            }
        },
    )


@app.exception_handler(Exception)
async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception(
        "unexpected_error",
        extra={"method": request.method, "path": request.url.path},
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "internal_error",
                "message": "服务发生未预期错误，请使用响应中的请求ID查询日志。",
                "details": [],
            }
        },
    )


app.include_router(health.router)
app.include_router(auth.router)
app.include_router(access.router)
app.include_router(admin.router)
app.include_router(ingestion.router)
app.include_router(dashboard.router)
app.include_router(read_models.router)
app.include_router(mtbf.router)
