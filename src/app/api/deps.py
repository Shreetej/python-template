"""Request-scoped dependencies. Shared resources live on `app.state` (created in lifespan)."""

from collections.abc import AsyncIterator
from typing import Annotated, Any, cast

import httpx
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.cache import Cache
from app.core.config import Settings
from app.core.exceptions import ServiceUnavailableError
from app.core.singleflight import SingleFlight
from app.db.clickhouse import ClickHouse
from app.services.analytics import AnalyticsService
from app.services.item import ItemService


def get_settings(request: Request) -> Settings:
    return cast("Settings", request.app.state.settings)


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    sessionmaker = cast("async_sessionmaker[AsyncSession]", request.app.state.sessionmaker)
    async with sessionmaker() as session:
        yield session
    # Leaving the block rolls back anything uncommitted and returns the
    # connection to the pool.


def get_cache(request: Request) -> Cache:
    return cast("Cache", request.app.state.cache)


def get_singleflight(request: Request) -> SingleFlight[Any]:
    return cast("SingleFlight[Any]", request.app.state.singleflight)


def get_http_client(request: Request) -> httpx.AsyncClient:
    """One pooled client per worker: reuses TCP/TLS connections across requests."""
    return cast("httpx.AsyncClient", request.app.state.http_client)


def get_clickhouse(request: Request) -> ClickHouse:
    clickhouse = cast("ClickHouse | None", request.app.state.clickhouse)
    if clickhouse is None:
        raise ServiceUnavailableError("Analytics backend not configured")
    return clickhouse


SettingsDep = Annotated[Settings, Depends(get_settings)]
# scope="function": the session closes (connection back to the pool) as soon as
# the endpoint returns, BEFORE the response is sent. With the default "request"
# scope, every request holds its DB connection while bytes are written to the
# client, so slow clients / large responses can exhaust the pool.
SessionDep = Annotated[AsyncSession, Depends(get_session, scope="function")]
CacheDep = Annotated[Cache, Depends(get_cache)]
SingleFlightDep = Annotated[SingleFlight[Any], Depends(get_singleflight)]
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]
ClickHouseDep = Annotated[ClickHouse, Depends(get_clickhouse)]


def get_item_service(session: SessionDep, cache: CacheDep, flight: SingleFlightDep) -> ItemService:
    return ItemService(session, cache, flight)


def get_analytics_service(
    clickhouse: ClickHouseDep, cache: CacheDep, flight: SingleFlightDep
) -> AnalyticsService:
    return AnalyticsService(clickhouse, cache, flight)


ItemServiceDep = Annotated[ItemService, Depends(get_item_service)]
AnalyticsServiceDep = Annotated[AnalyticsService, Depends(get_analytics_service)]
