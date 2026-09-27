"""Every error has one envelope; infra failures map to retryable 503/504, bugs to 500."""

from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError as CHDatabaseError
from clickhouse_connect.driver.exceptions import OperationalError as CHOperationalError
from clickhouse_connect.driver.exceptions import ProgrammingError as CHProgrammingError
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.config import Settings
from app.core.exceptions import (
    ServiceUnavailableError,
    UpstreamTimeoutError,
    classify_clickhouse_error,
    classify_db_error,
)
from tests.conftest import running_app


def _assert_envelope(body: dict[str, Any], code: str) -> None:
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    assert len(body["error"]["request_id"]) == 32


async def test_validation_error_envelope(client: AsyncClient) -> None:
    resp = await client.post("/api/v1/items", json={"name": "", "price": "-1"})
    assert resp.status_code == 422
    body = resp.json()
    _assert_envelope(body, "validation_error")
    assert {e["loc"][-1] for e in body["error"]["details"]} == {"name", "price"}


async def test_http_errors_share_envelope(client: AsyncClient) -> None:
    resp = await client.get("/nope")
    assert resp.status_code == 404
    _assert_envelope(resp.json(), "http_error")

    resp = await client.put("/api/v1/items")
    assert resp.status_code == 405
    _assert_envelope(resp.json(), "http_error")


async def test_domain_error_envelope(client: AsyncClient) -> None:
    resp = await client.get("/api/v1/items/999")
    assert resp.status_code == 404
    _assert_envelope(resp.json(), "not_found")


async def test_pool_exhaustion_returns_503_with_retry_after(settings: Settings) -> None:
    def setup(app: FastAPI) -> None:
        @app.get("/pool")
        async def pool() -> None:
            raise PoolTimeoutError("QueuePool limit reached")

    async with running_app(settings, setup) as (_, client):
        resp = await client.get("/pool")
    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    _assert_envelope(resp.json(), "service_unavailable")


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


@pytest.mark.parametrize(
    ("sqlstate", "invalidated", "expected"),
    [
        ("57014", False, UpstreamTimeoutError),  # statement_timeout
        ("57P01", False, ServiceUnavailableError),  # admin shutdown / failover
        ("53300", False, ServiceUnavailableError),  # too many connections
        ("XX000", True, ServiceUnavailableError),  # connection dropped mid-query
        ("42P01", False, None),  # undefined table: a bug -> 500
    ],
)
def test_classify_db_error(sqlstate: str, invalidated: bool, expected: type | None) -> None:
    exc = DBAPIError("SELECT 1", None, _PgError(sqlstate), connection_invalidated=invalidated)
    mapped = classify_db_error(exc)
    assert (type(mapped) if mapped else None) is expected


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (CHDatabaseError("timeout", code=159), UpstreamTimeoutError),
        (CHDatabaseError("too many queries", code=202), ServiceUnavailableError),
        (CHDatabaseError("memory", code=241), ServiceUnavailableError),
        (CHOperationalError("Network Error: connection refused"), ServiceUnavailableError),
        (CHDatabaseError("Syntax error", code=62), None),  # bug -> 500
        (CHDatabaseError("readonly", code=164), None),  # bug -> 500
        (CHProgrammingError("bad params"), None),
    ],
)
def test_classify_clickhouse_error(exc: Exception, expected: type | None) -> None:
    mapped = classify_clickhouse_error(exc)  # type: ignore[arg-type]
    assert (type(mapped) if mapped else None) is expected
