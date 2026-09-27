from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

import anyio.to_thread
import httpx
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from redis.asyncio import BlockingConnectionPool, Redis

from app.api.v1.router import api_router
from app.api.v1.routes import health
from app.core.cache import Cache
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.singleflight import SingleFlight
from app.db.clickhouse import ClickHouse
from app.db.session import create_engine, create_sessionmaker

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    """Create pooled resources once per worker process (after fork); close on shutdown.

    AsyncExitStack: if any resource fails to start, the ones already opened are
    still closed, and shutdown runs in reverse order of creation.
    """
    settings: Settings = app.state.settings

    # Capacity for sync `def` endpoints/deps (run in a threadpool).
    anyio.to_thread.current_default_thread_limiter().total_tokens = settings.thread_pool_size

    async with AsyncExitStack() as stack:
        engine = create_engine(settings)
        stack.push_async_callback(engine.dispose)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)

        redis: Redis | None = None
        if settings.redis_url:
            pool = BlockingConnectionPool.from_url(
                settings.redis_url,
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                socket_connect_timeout=settings.redis_socket_timeout,
                timeout=settings.redis_socket_timeout,  # wait for a free pooled conn
                health_check_interval=30,
            )
            redis = Redis(connection_pool=pool)
            # We own the pool, so redis-py won't close it unless told to: without
            # this, every shutdown/reload leaks the pool's sockets.
            stack.push_async_callback(redis.aclose, close_connection_pool=True)
        app.state.redis = redis
        app.state.cache = Cache(
            redis, default_ttl=settings.cache_ttl_seconds, namespace=settings.name
        )
        app.state.singleflight = SingleFlight[Any]()

        clickhouse: ClickHouse | None = None
        if settings.clickhouse_url:
            clickhouse = ClickHouse(settings)
            stack.push_async_callback(clickhouse.close)
            try:
                await clickhouse.connect()  # warm up; failure is not fatal
            except Exception as exc:  # noqa: BLE001
                logger.warning("clickhouse_unavailable_at_startup", error=str(exc)[:300])
        app.state.clickhouse = clickhouse

        http_client = httpx.AsyncClient(
            timeout=settings.http_timeout,
            limits=httpx.Limits(
                max_connections=settings.http_max_connections,
                max_keepalive_connections=settings.http_max_keepalive,
            ),
        )
        stack.push_async_callback(http_client.aclose)
        app.state.http_client = http_client

        logger.info(
            "startup",
            environment=settings.environment,
            clickhouse=clickhouse is not None,
            redis=redis is not None,
        )
        yield
        logger.info("shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.name,
        debug=settings.debug,
        lifespan=lifespan,
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )
    app.state.settings = settings

    # Middleware executes outermost-last-added. Keep the stack short: each layer
    # is paid on every request.
    app.add_middleware(GZipMiddleware, minimum_size=settings.gzip_min_size)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(RequestContextMiddleware, skip_paths=settings.access_log_skip_paths)

    register_exception_handlers(app)
    app.include_router(health.router)
    app.include_router(api_router, prefix=settings.api_prefix)

    if settings.metrics_enabled:
        # Multi-worker: set PROMETHEUS_MULTIPROC_DIR so every worker's metrics aggregate.
        Instrumentator(
            excluded_handlers=["/health/.*", "/metrics"],
            should_group_status_codes=False,
        ).instrument(app).expose(app, include_in_schema=False)

    return app


app = create_app()
