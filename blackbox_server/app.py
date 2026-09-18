from __future__ import annotations

import json
import logging
import time
import uuid
import secrets
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
from sqlalchemy.exc import SQLAlchemyError

from .api import router
from .artifacts import AzureBlobArtifactStore, LocalArtifactStore
from .config import ServerSettings
from .db import create_database
from .errors import Problem, problem_response

logger = logging.getLogger("blackbox_server")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                **getattr(record, "fields", {}),
            },
            separators=(",", ":"),
            default=str,
        )


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    logger.handlers[:] = [handler]
    logger.setLevel(level.upper())
    logger.propagate = False


def create_app(settings: ServerSettings | None = None) -> FastAPI:
    settings = settings or ServerSettings()
    configure_logging(settings.log_level)
    engine, factory = create_database(settings)
    if settings.artifact_backend == "azure":
        if not settings.azure_blob_account_url:
            raise ValueError("azure_blob_account_url is required for Azure artifacts")
        artifacts = AzureBlobArtifactStore(
            settings.azure_blob_account_url, settings.azure_blob_container
        )
    elif settings.artifact_backend == "local":
        configured = settings.local_signing_secret
        if configured is None:
            if settings.environment not in {"development", "test"}:
                raise ValueError("local artifact signing is not configured")
            signing_key = secrets.token_bytes(32)
        else:
            signing_key = configured.get_secret_value().encode()
        artifacts = LocalArtifactStore(
            settings.artifact_root,
            signing_key,
            tuple(
                item.get_secret_value().encode()
                for item in settings.local_previous_signing_secrets
            ),
        )
    else:
        raise ValueError(f"unsupported artifact backend: {settings.artifact_backend}")

    app = FastAPI(
        title="Agent Black Box API",
        version=settings.service_version,
        description="Optional self-hosted service. Identity verification is added in Phase 7.",
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = factory
    app.state.artifact_store = artifacts
    app.include_router(router)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        supplied = request.headers.get("x-request-id")
        request_id = supplied if supplied and len(supplied) <= 128 else str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed",
                extra={"fields": {"request_id": request_id, "path": request.url.path}},
            )
            raise
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_completed",
            extra={
                "fields": {
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                }
            },
        )
        return response

    @app.exception_handler(Problem)
    async def handle_problem(request: Request, exc: Problem):
        return problem_response(request, exc)

    @app.exception_handler(RequestValidationError)
    async def handle_validation(request: Request, exc: RequestValidationError):
        errors = [
            {key: value for key, value in item.items() if key not in {"ctx", "input", "url"}}
            for item in exc.errors()
        ]
        problem = Problem(
            422,
            "Validation failed",
            "The request did not satisfy the API contract.",
            extensions={"errors": errors},
        )
        return problem_response(request, problem)

    @app.exception_handler(SQLAlchemyError)
    async def handle_database(request: Request, exc: SQLAlchemyError):
        logger.error(
            "database_error",
            extra={"fields": {"request_id": getattr(request.state, "request_id", None)}},
        )
        return problem_response(
            request,
            Problem(503, "Database unavailable", "The database operation could not be completed."),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        return problem_response(
            request,
            Problem(500, "Internal server error", "The request could not be completed."),
        )

    @app.get("/health", tags=["service"])
    def health():
        return {"status": "ok", "version": settings.service_version}

    @app.get("/ready", tags=["service"])
    def ready(response: Response):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return {"status": "ready", "database": "available"}
        except SQLAlchemyError:
            response.status_code = 503
            return {"status": "not_ready", "database": "unavailable"}

    @app.get("/migration-status", tags=["service"])
    def migration_status():
        with engine.connect() as connection:
            tables = inspect(connection).get_table_names()
            revision = None
            if "alembic_version" in tables:
                revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        return {
            "revision": revision,
            "current": revision == "0001_phase6",
            "expected": "0001_phase6",
        }

    @app.get("/version", tags=["service"])
    def version():
        return {"version": settings.service_version}

    return app
