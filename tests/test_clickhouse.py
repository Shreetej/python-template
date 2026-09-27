"""ClickHouse wrapper: bulkhead, deadlines, lazy reconnect, endpoint behaviour (fake client)."""

import asyncio
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import OperationalError

import app.db.clickhouse as ch_module
from app.core.config import Settings
from app.core.exceptions import ServiceUnavailableError, UpstreamTimeoutError
from app.db.clickhouse import ClickHouse
from tests.conftest import running_app


@dataclass
class _Result:
    column_names: tuple[str, ...]
    result_rows: list[tuple[Any, ...]]


@dataclass
class FakeClient:
    delay: float = 0.0
    calls: int = 0
    settings_seen: list[dict[str, Any]] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0

    async def query(self, _sql: str, parameters: Any = None, settings: Any = None) -> _Result:
        self.calls += 1
        self.settings_seen.append(settings or {})
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.in_flight -= 1
        return _Result(
            ("day", "views", "purchases", "revenue"),
            [(date(2026, 9, 1), 10, 2, Decimal("19.98"))],
        )

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        pass


def _clickhouse(settings: Settings, client: FakeClient, **overrides: Any) -> ClickHouse:
    ch = ClickHouse(settings.model_copy(update={"clickhouse_url": "http://x:8123", **overrides}))
    ch._client = client  # type: ignore[assignment]
    return ch


async def test_bulkhead_caps_concurrency_and_sheds_excess(settings: Settings) -> None:
    fake = FakeClient(delay=0.3)
    ch = _clickhouse(settings, fake, clickhouse_max_concurrency=2, clickhouse_queue_timeout=0.05)

    results = await asyncio.gather(
        *(ch.query("SELECT 1") for _ in range(5)), return_exceptions=True
    )

    ok = [r for r in results if isinstance(r, list)]
    shed = [r for r in results if isinstance(r, ServiceUnavailableError)]
    assert (len(ok), len(shed)) == (2, 3)
    assert fake.max_in_flight == 2  # ClickHouse never saw more than the cap


async def test_query_deadline_and_slot_release(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ch_module, "_CLIENT_DEADLINE_SLACK", 0.0)
    fake = FakeClient(delay=5)
    ch = _clickhouse(settings, fake, clickhouse_max_concurrency=1, clickhouse_query_timeout=0.05)

    with pytest.raises(UpstreamTimeoutError):
        await ch.query("SELECT 1")

    fake.delay = 0  # slot must have been released despite the timeout
    assert await ch.query("SELECT 1")


async def test_server_side_limits_sent_with_every_query(settings: Settings) -> None:
    fake = FakeClient()
    ch = _clickhouse(settings, fake, clickhouse_query_timeout=7, clickhouse_max_rows=500)
    await ch.query("SELECT 1", settings={"max_threads": 2})
    assert fake.settings_seen[0] == {
        "max_execution_time": 7,
        "max_result_rows": 500,
        "result_overflow_mode": "throw",
        "max_threads": 2,
    }


async def test_reconnect_is_lazy_with_cooldown(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    async def refuse(_: Settings) -> Any:
        nonlocal attempts
        attempts += 1
        raise OperationalError("Network Error: connection refused")

    monkeypatch.setattr(ch_module, "create_clickhouse_client", refuse)
    ch = ClickHouse(settings.model_copy(update={"clickhouse_url": "http://x:8123"}))

    with pytest.raises(OperationalError):
        await ch.query("SELECT 1")
    # Within the cooldown: fail fast, no new connect attempt per request.
    for _ in range(10):
        with pytest.raises(ServiceUnavailableError):
            await ch.query("SELECT 1")
    assert attempts == 1


async def test_analytics_endpoint_coalesces_concurrent_requests(settings: Settings) -> None:
    fake = FakeClient(delay=0.05)
    async with running_app(settings) as (app, client):
        app.state.clickhouse = _clickhouse(settings, fake)
        responses = await asyncio.gather(
            *(client.get("/api/v1/analytics/items/7/daily?days=30") for _ in range(20))
        )

    assert all(r.status_code == 200 for r in responses)
    assert responses[0].json() == {
        "item_id": 7,
        "days": 30,
        "series": [{"day": "2026-09-01", "views": 10, "purchases": 2, "revenue": "19.98"}],
    }
    assert fake.calls == 1  # 20 concurrent requests -> 1 ClickHouse query


async def test_analytics_without_clickhouse_is_503(client: Any) -> None:
    resp = await client.get("/api/v1/analytics/items/7/daily")
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "service_unavailable"


async def test_api_boots_when_clickhouse_is_down(settings: Settings) -> None:
    settings = settings.model_copy(update={"clickhouse_url": "http://default:@127.0.0.1:1/default"})
    async with running_app(settings) as (_, client):
        assert (await client.get("/api/v1/items")).status_code == 200  # OLTP unaffected
        resp = await client.get("/api/v1/analytics/items/1/daily")
        assert resp.status_code == 503
        ready = await client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["checks"] == {"database": "ok", "clickhouse": "error"}
