"""Integration tests against real Postgres, Redis and ClickHouse.

    docker compose up -d postgres redis clickhouse
    uv run pytest -m integration

(If a local Postgres owns 5432: export POSTGRES_HOST_PORT=5433 for both commands.)

Skipped automatically when the services aren't reachable.
"""

import asyncio
import os
import socket
import time
import uuid
from typing import Any

import asyncpg
import pytest
from fastapi import FastAPI
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.deps import ClickHouseDep, SessionDep
from app.core.config import Settings
from tests.conftest import running_app

PG_PORT = int(os.environ.get("POSTGRES_HOST_PORT", "5432"))
PG_URL = f"postgresql+asyncpg://app:app@localhost:{PG_PORT}/app"
REDIS_URL = "redis://localhost:6379/15"
CH_URL = "http://api:api@localhost:8123/default"


def _reachable(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _postgres_ready() -> bool:
    """Port open is not enough: another local Postgres may own it."""

    async def probe() -> bool:
        conn = await asyncpg.connect(
            "postgresql://app:app@localhost:" + str(PG_PORT) + "/app", timeout=1
        )
        await conn.close()
        return True

    try:
        return asyncio.run(probe())
    except (OSError, asyncpg.PostgresError, TimeoutError):
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (all(_reachable(p) for p in (PG_PORT, 6379, 8123)) and _postgres_ready()),
        reason="docker compose services not running (set POSTGRES_HOST_PORT if remapped)",
    ),
]


@pytest.fixture
def live(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "name": f"it-{uuid.uuid4().hex[:8]}",  # unique app name / cache namespace
            "database_url": PG_URL,
            "redis_url": REDIS_URL,
            "clickhouse_url": CH_URL,
        }
    )


def _db_routes(app: FastAPI) -> None:
    @app.get("/sleep/{seconds}")
    async def sleep(seconds: float, session: SessionDep) -> dict[str, str]:
        await session.execute(text("SELECT pg_sleep(:s)"), {"s": seconds})
        return {"ok": "yes"}

    @app.get("/ch-slow")
    async def ch_slow(ch: ClickHouseDep) -> list[dict[str, Any]]:
        return await ch.query(
            "SELECT sleepEachRow(0.5) FROM numbers(10)",
            settings={"max_execution_time": 1, "max_block_size": 1},
        )

    @app.get("/ch-write")
    async def ch_write(ch: ClickHouseDep) -> list[dict[str, Any]]:
        return await ch.query("CREATE TABLE should_fail (x UInt8) ENGINE = Memory")


async def _kill_app_connections(app_name: str) -> int:
    admin = create_async_engine(PG_URL)
    async with admin.connect() as conn:
        killed = await conn.scalar(
            text(
                "SELECT count(pg_terminate_backend(pid)) FROM pg_stat_activity "
                "WHERE application_name = :n"
            ),
            {"n": app_name},
        )
    await admin.dispose()
    return int(killed or 0)


# --- Postgres ---------------------------------------------------------------


async def test_crud_with_redis_cache(live: Settings) -> None:
    async with running_app(live) as (app, client):
        name = f"item-{uuid.uuid4().hex}"
        item = (await client.post("/api/v1/items", json={"name": name, "price": "5.00"})).json()

        selects: list[str] = []

        @event.listens_for(app.state.engine.sync_engine, "before_cursor_execute")
        def count(_c: Any, _cur: Any, statement: str, *_: Any) -> None:
            if "FROM items" in statement:
                selects.append(statement)

        for _ in range(5):
            assert (await client.get(f"/api/v1/items/{item['id']}")).json()["name"] == name
        assert len(selects) == 1  # 1 miss, then Redis
        assert await app.state.redis.exists(f"{live.name}:item:{item['id']}")

        await client.patch(f"/api/v1/items/{item['id']}", json={"price": "6.00"})
        assert (await client.get(f"/api/v1/items/{item['id']}")).json()["price"] == "6.00"


async def test_statement_timeout_maps_to_504(live: Settings) -> None:
    live = live.model_copy(update={"db_statement_timeout_ms": 200})
    async with running_app(live, _db_routes) as (app, client):
        start = time.perf_counter()
        resp = await client.get("/sleep/3")
        assert time.perf_counter() - start < 1.0  # Postgres killed it at 200ms
        assert resp.status_code == 504
        assert resp.json()["error"]["code"] == "upstream_timeout"
        assert app.state.engine.pool.checkedout() == 0
        assert (await client.get("/sleep/0")).status_code == 200  # connection reusable


async def test_pool_exhaustion_sheds_load_fast(live: Settings) -> None:
    live = live.model_copy(update={"db_pool_size": 1, "db_max_overflow": 0, "db_pool_timeout": 0.2})
    async with running_app(live, _db_routes) as (_, client):
        start = time.perf_counter()
        responses = await asyncio.gather(*(client.get("/sleep/0.8") for _ in range(3)))
        elapsed = time.perf_counter() - start

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 503, 503]
    assert all(r.headers["retry-after"] == "1" for r in responses if r.status_code == 503)
    assert elapsed < 1.5  # rejected after pool_timeout, not queued behind the sleeper


async def test_recovers_transparently_after_connections_killed(live: Settings) -> None:
    """DB failover / restart / idle-connection reaping by a proxy (pre_ping=True)."""
    async with running_app(live) as (_, client):
        assert (await client.get("/api/v1/items")).status_code == 200
        assert await _kill_app_connections(live.name) >= 1
        assert (await client.get("/api/v1/items")).status_code == 200  # no failed request


async def test_without_pre_ping_dead_connection_is_503_then_recovers(live: Settings) -> None:
    live = live.model_copy(update={"db_pool_pre_ping": False})
    async with running_app(live) as (_, client):
        assert (await client.get("/api/v1/items")).status_code == 200
        await _kill_app_connections(live.name)
        resp = await client.get("/api/v1/items")
        assert resp.status_code == 503  # retryable, not a 500
        assert (await client.get("/api/v1/items")).status_code == 200  # pool self-heals


async def test_many_concurrent_requests_through_small_pool(live: Settings) -> None:
    live = live.model_copy(update={"db_pool_size": 5, "db_max_overflow": 0})
    async with running_app(live) as (app, client):
        responses = await asyncio.gather(*(client.get("/api/v1/items") for _ in range(300)))
        assert all(r.status_code == 200 for r in responses)
        assert app.state.engine.pool.checkedout() == 0


# --- Redis ------------------------------------------------------------------


async def test_redis_down_degrades_gracefully(live: Settings) -> None:
    live = live.model_copy(update={"redis_url": "redis://localhost:6390/0"})
    async with running_app(live) as (_, client):
        name = f"item-{uuid.uuid4().hex}"
        item = (await client.post("/api/v1/items", json={"name": name, "price": "1"})).json()
        assert (await client.get(f"/api/v1/items/{item['id']}")).status_code == 200
        ready = await client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"]["redis"] == "error"


# --- ClickHouse ---------------------------------------------------------------


async def test_clickhouse_analytics_endpoint(live: Settings) -> None:
    async with running_app(live) as (_, client):
        resp = await client.get("/api/v1/analytics/items/1/daily?days=7")
        assert resp.status_code == 200
        body = resp.json()
        assert body["item_id"] == 1
        assert 7 <= len(body["series"]) <= 8
        assert all(day["views"] > 0 for day in body["series"])
        assert (await client.get("/health/ready")).json()["checks"]["clickhouse"] == "ok"


async def test_clickhouse_server_timeout_maps_to_504(live: Settings) -> None:
    async with running_app(live, _db_routes) as (_, client):
        start = time.perf_counter()
        resp = await client.get("/ch-slow")
        assert resp.status_code == 504
        assert time.perf_counter() - start < 3  # ClickHouse cancelled it server-side


async def test_clickhouse_user_is_read_only(live: Settings) -> None:
    async with running_app(live, _db_routes) as (_, client):
        resp = await client.get("/ch-write")
        assert resp.status_code == 500  # a bug, not a retryable outage
        assert resp.json()["error"]["code"] == "internal_error"


async def test_clickhouse_concurrent_load(live: Settings) -> None:
    async with running_app(live) as (_, client):
        responses = await asyncio.gather(
            *(client.get(f"/api/v1/analytics/items/{i % 50 + 1}/daily?days=30") for i in range(200))
        )
    assert all(r.status_code == 200 for r in responses)
