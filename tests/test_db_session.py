"""Connection handling: release early, never leak, coalesce stampedes."""

import asyncio
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.deps import SessionDep, get_session
from app.core.config import Settings
from tests.conftest import running_app

# Negative control: FastAPI's default dependency scope ("request").
RequestScopedSession = Annotated[AsyncSession, Depends(get_session)]


def _checked_out_while_streaming(app: FastAPI) -> None:
    def body(request: Request) -> StreamingResponse:
        engine: AsyncEngine = request.app.state.engine

        async def stream() -> AsyncIterator[bytes]:
            # Runs while the response is being sent to the client.
            yield str(engine.pool.checkedout()).encode()  # type: ignore[attr-defined]

        return StreamingResponse(stream())

    @app.get("/function-scope")
    async def function_scope(session: SessionDep, request: Request) -> StreamingResponse:
        await session.execute(text("SELECT 1"))
        return body(request)

    @app.get("/request-scope")
    async def request_scope(session: RequestScopedSession, request: Request) -> StreamingResponse:
        await session.execute(text("SELECT 1"))
        return body(request)


async def test_connection_released_before_response_is_sent(settings: Settings) -> None:
    async with running_app(settings, _checked_out_while_streaming) as (app, client):
        # Our SessionDep: connection already back in the pool while sending.
        assert (await client.get("/function-scope")).text == "0"
        # Control: default scope holds it for the whole send -> slow clients
        # (or big responses) pin pool connections. This is what we avoid.
        assert (await client.get("/request-scope")).text == "1"
        assert app.state.engine.pool.checkedout() == 0  # nothing leaked


async def test_connection_released_when_handler_raises(settings: Settings) -> None:
    def setup(app: FastAPI) -> None:
        @app.get("/fail")
        async def fail(session: SessionDep) -> None:
            await session.execute(text("SELECT 1"))
            raise RuntimeError("bug")

    async with running_app(settings, setup) as (app, client):
        for _ in range(5):
            assert (await client.get("/fail")).status_code == 500
        assert app.state.engine.pool.checkedout() == 0


async def test_cache_miss_stampede_hits_db_once(settings: Settings) -> None:
    async with running_app(settings) as (app, client):
        created = (await client.post("/api/v1/items", json={"name": "hot", "price": "1"})).json()

        selects: list[str] = []

        @event.listens_for(app.state.engine.sync_engine, "before_cursor_execute")
        def count(_c: Any, _cur: Any, statement: str, *_: Any) -> None:
            if statement.lstrip().upper().startswith("SELECT") and "items" in statement:
                selects.append(statement)

        # No Redis in this test, so every request is a cache miss.
        responses = await asyncio.gather(
            *(client.get(f"/api/v1/items/{created['id']}") for _ in range(50))
        )
        assert all(r.status_code == 200 for r in responses)
        assert len(selects) == 1, f"{len(selects)} DB queries for 50 concurrent misses"
