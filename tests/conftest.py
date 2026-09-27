from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TextIO

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.core.logging import configure_logging
from app.db.base import Base
from app.main import create_app


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        redis_url=None,
        clickhouse_url=None,
        log_json=True,
        log_level="WARNING",
        metrics_enabled=False,
    )


@asynccontextmanager
async def running_app(
    settings: Settings,
    setup: Callable[[FastAPI], None] | None = None,
    log_stream: TextIO | None = None,
) -> AsyncGenerator[tuple[FastAPI, AsyncClient]]:
    """Real app + real lifespan, driven in-process (no network, no server)."""
    app = create_app(settings)
    if log_stream is not None:
        configure_logging(settings, stream=log_stream)
    if setup is not None:
        setup(app)  # test-only routes
    async with LifespanManager(app) as manager:
        engine: AsyncEngine = app.state.engine
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield app, client


@pytest.fixture
async def client(settings: Settings) -> AsyncIterator[AsyncClient]:
    async with running_app(settings) as (_, ac):
        yield ac
