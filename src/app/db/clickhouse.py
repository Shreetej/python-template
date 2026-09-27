"""ClickHouse read client.

Uses the official `clickhouse-connect` AsyncClient: aiohttp for non-blocking IO,
the native binary format (RowBinary/Native + LZ4), and result decoding off the
event loop in a worker thread, so large results don't freeze other requests.

Protections, from cheapest to most expensive:
1. Bulkhead: at most `max_concurrency` in-flight queries per worker; waiting for a
   slot is bounded by `queue_timeout` -> 503. Analytics spikes can't starve OLTP.
2. Server-side limits sent with every query: `max_execution_time` (ClickHouse
   cancels the query and frees its CPU/RAM) and `max_result_rows`.
3. Client-side deadline slightly above the server one, in case the network or
   server stalls.
4. Lazy (re)connect: creating the client contacts the server, so a ClickHouse
   outage must not stop the API from booting (Postgres endpoints keep working).
   After a failed connect, requests fail fast (503) for a short cooldown instead
   of every request paying a connect timeout.
Always bind values with `{name:Type}` placeholders (server-side binding); never
format user input into SQL.
"""

import asyncio
import time
from collections.abc import Mapping, Sequence
from typing import Any, cast

from clickhouse_connect.driver import create_async_client
from clickhouse_connect.driver.asyncclient import AsyncClient

from app.core.config import Settings
from app.core.exceptions import ServiceUnavailableError, UpstreamTimeoutError

_CLIENT_DEADLINE_SLACK = 2.0
_RECONNECT_COOLDOWN = 1.0


async def create_clickhouse_client(settings: Settings) -> AsyncClient:
    assert settings.clickhouse_url  # noqa: S101 - caller checks
    return await create_async_client(
        dsn=settings.clickhouse_url,
        connect_timeout=settings.clickhouse_connect_timeout,
        send_receive_timeout=settings.clickhouse_query_timeout + _CLIENT_DEADLINE_SLACK,
        # aiohttp pool sized to the bulkhead: every admitted query gets a connection.
        connector_limit=settings.clickhouse_max_concurrency,
        connector_limit_per_host=settings.clickhouse_max_concurrency,
        # Stateless HTTP: no session id, so queries on one client run in parallel
        # (ClickHouse serializes queries that share a session).
        autogenerate_session_id=False,
        client_name=settings.name,
    )


class ClickHouse:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: AsyncClient | None = None
        self._connect_lock = asyncio.Lock()
        self._connect_blocked_until = 0.0
        self._slots = asyncio.Semaphore(settings.clickhouse_max_concurrency)
        self._queue_timeout = settings.clickhouse_queue_timeout
        self._deadline = settings.clickhouse_query_timeout + _CLIENT_DEADLINE_SLACK
        self._query_settings: dict[str, Any] = {
            "max_execution_time": settings.clickhouse_query_timeout,
            "max_result_rows": settings.clickhouse_max_rows,
            "result_overflow_mode": "throw",
        }

    async def connect(self) -> AsyncClient:
        if self._client is not None:
            return self._client
        async with self._connect_lock:
            if self._client is None:
                if time.monotonic() < self._connect_blocked_until:
                    raise ServiceUnavailableError("Analytics backend unavailable")
                try:
                    self._client = await create_clickhouse_client(self._settings)
                except Exception:
                    self._connect_blocked_until = time.monotonic() + _RECONNECT_COOLDOWN
                    raise
            return self._client

    async def query(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        column_names, rows = await self.query_rows(sql, parameters, settings)
        return [dict(zip(column_names, row, strict=True)) for row in rows]

    async def query_rows(
        self,
        sql: str,
        parameters: Mapping[str, Any] | None = None,
        settings: Mapping[str, Any] | None = None,
    ) -> tuple[Sequence[str], Sequence[Sequence[Any]]]:
        try:
            async with asyncio.timeout(self._queue_timeout):
                await self._slots.acquire()
        except TimeoutError:
            raise ServiceUnavailableError("Analytics capacity exhausted, retry") from None

        try:
            async with asyncio.timeout(self._deadline):
                client = await self.connect()
                result = await client.query(
                    sql,
                    parameters=dict(parameters) if parameters else None,
                    settings=self._query_settings | dict(settings or {}),
                )
        except TimeoutError:
            raise UpstreamTimeoutError("Analytics query timed out") from None
        finally:
            self._slots.release()
        return cast("Sequence[str]", result.column_names), result.result_rows

    async def ping(self) -> bool:
        return bool(await (await self.connect()).ping())

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
