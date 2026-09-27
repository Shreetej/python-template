"""Domain errors, infrastructure-error mapping, and one error envelope for everything.

Every error response has the same shape (clients parse one format):

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": ...}}

Mapping principles for a high-concurrency service:
- Overload / dependency down  -> 503 + Retry-After (client may retry; LB may shed)
- Dependency too slow         -> 504 (never hang: every wait has a deadline)
- Our bug                     -> 500, logged once with traceback (see middleware)
Expected client errors (4xx) are not logged as errors: they are not incidents.
"""

from typing import Any, ClassVar

import structlog
from clickhouse_connect.driver.exceptions import ClickHouseError
from clickhouse_connect.driver.exceptions import DatabaseError as ClickHouseDatabaseError
from clickhouse_connect.driver.exceptions import OperationalError as ClickHouseOperationalError
from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = structlog.get_logger(__name__)

# Postgres SQLSTATEs
_PG_QUERY_CANCELED = "57014"  # statement_timeout
_PG_ADMIN_SHUTDOWN = "57P01"
_PG_CANNOT_CONNECT = "57P03"
_PG_TOO_MANY_CONNECTIONS = "53300"

# ClickHouse error codes
_CH_TIMEOUT_EXCEEDED = 159
_CH_TOO_MANY_SIMULTANEOUS_QUERIES = 202
_CH_MEMORY_LIMIT_EXCEEDED = 241


class AppError(Exception):
    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    headers: ClassVar[dict[str, str] | None] = None

    def __init__(self, message: str = "Internal server error", details: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    headers: ClassVar[dict[str, str] | None] = {"Retry-After": "1"}


class UpstreamTimeoutError(AppError):
    status_code = status.HTTP_504_GATEWAY_TIMEOUT
    code = "upstream_timeout"


def error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: object = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "code": code,
        "message": message,
        "request_id": getattr(request.state, "request_id", None),
    }
    if details is not None:
        body["details"] = jsonable_encoder(details)
    return JSONResponse({"error": body}, status_code=status_code, headers=headers)


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = exc.orig
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


def classify_db_error(exc: DBAPIError) -> AppError | None:
    """Map transient/infra Postgres failures to 503/504; None = genuine bug (500)."""
    state = _sqlstate(exc)
    if state == _PG_QUERY_CANCELED:
        return UpstreamTimeoutError("Database query timed out")
    if exc.connection_invalidated or state in {
        _PG_ADMIN_SHUTDOWN,
        _PG_CANNOT_CONNECT,
        _PG_TOO_MANY_CONNECTIONS,
    }:
        return ServiceUnavailableError("Database unavailable")
    if isinstance(exc.orig, (ConnectionError, OSError)):
        return ServiceUnavailableError("Database unavailable")
    return None


def classify_clickhouse_error(exc: ClickHouseError) -> AppError | None:
    """Map transient ClickHouse failures to 503/504; None = genuine bug (500)."""
    code = exc.code if isinstance(exc, ClickHouseDatabaseError) else None
    if code == _CH_TIMEOUT_EXCEEDED:
        return UpstreamTimeoutError("Analytics query timed out")
    if code in {_CH_TOO_MANY_SIMULTANEOUS_QUERIES, _CH_MEMORY_LIMIT_EXCEEDED}:
        return ServiceUnavailableError("Analytics backend overloaded")
    if code is None and isinstance(exc, ClickHouseOperationalError):
        return ServiceUnavailableError("Analytics backend unavailable")  # network
    # Syntax errors, unknown tables, READONLY violations, result-too-large: our bug.
    return None


async def _app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, AppError):  # pragma: no cover - registered for AppError only
        raise exc
    if exc.status_code >= status.HTTP_500_INTERNAL_SERVER_ERROR:
        logger.warning("request_failed", code=exc.code, error=exc.message)
    return error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
        headers=exc.headers,
    )


async def _http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)  # noqa: S101 - handler registration
    return error_response(
        request,
        status_code=exc.status_code,
        code="http_error",
        message=str(exc.detail),
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    return error_response(
        request,
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        code="validation_error",
        message="Request validation failed",
        details=exc.errors(),
    )


async def _pool_timeout_handler(request: Request, exc: Exception) -> JSONResponse:
    # Every pooled connection is busy for longer than pool_timeout: shed load.
    logger.warning("db_pool_exhausted", error=str(exc))
    return await _app_error_handler(request, ServiceUnavailableError("Server busy, retry"))


async def _dbapi_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, DBAPIError)  # noqa: S101
    mapped = classify_db_error(exc)
    if mapped is None:
        raise exc  # real bug: let the middleware log it with a traceback -> 500
    logger.warning("db_error", sqlstate=_sqlstate(exc), error=str(exc.orig))
    return await _app_error_handler(request, mapped)


async def _clickhouse_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ClickHouseError)  # noqa: S101
    mapped = classify_clickhouse_error(exc)
    if mapped is None:
        raise exc  # real bug: middleware logs traceback -> 500
    logger.warning("clickhouse_error", error=str(exc)[:500])
    return await _app_error_handler(request, mapped)


def register_exception_handlers(app: FastAPI) -> None:
    # Unhandled exceptions are handled by RequestContextMiddleware (it sits inside
    # Starlette's ServerErrorMiddleware, so the 500 still carries x-request-id and
    # the traceback is logged exactly once).
    app.add_exception_handler(AppError, _app_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(PoolTimeoutError, _pool_timeout_handler)
    app.add_exception_handler(DBAPIError, _dbapi_error_handler)
    app.add_exception_handler(ClickHouseError, _clickhouse_error_handler)
