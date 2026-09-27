from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings


def create_engine(settings: Settings) -> AsyncEngine:
    kwargs: dict[str, Any] = {"echo": settings.db_echo}

    if not settings.is_sqlite:
        kwargs |= {
            "pool_size": settings.db_pool_size,
            "max_overflow": settings.db_max_overflow,
            # Fail fast when the pool is exhausted instead of piling up waiters.
            "pool_timeout": settings.db_pool_timeout,
            "pool_recycle": settings.db_pool_recycle,
            "pool_pre_ping": settings.db_pool_pre_ping,
            # LIFO keeps a small hot set of connections busy and lets idle extras
            # age out (pool_recycle) instead of round-robining through all of them.
            "pool_use_lifo": True,
            "connect_args": {
                "server_settings": {
                    "application_name": settings.name,
                    # Kill runaway queries server-side.
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "idle_in_transaction_session_timeout": str(
                        settings.db_idle_in_transaction_timeout_ms
                    ),
                    # JIT compilation costs 10-100ms and only pays off for big
                    # analytical queries (those go to ClickHouse); off for OLTP.
                    "jit": "off",
                },
                "timeout": settings.db_pool_timeout,  # TCP connect timeout
                # asyncpg caches prepared statements per connection. If you run
                # behind PgBouncer in transaction mode, set this to 0.
                "statement_cache_size": 1024,
            },
        }

    return create_async_engine(settings.database_url, **kwargs)


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: objects stay usable after commit with no lazy re-SELECT
    # (lazy IO is impossible under asyncio anyway).
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
