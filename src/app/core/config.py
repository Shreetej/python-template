"""Typed application settings loaded from environment variables / `.env`."""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="APP_",
        extra="ignore",
    )

    # --- General -------------------------------------------------------------
    name: str = "fastapi-template"
    environment: Literal["local", "test", "staging", "production"] = "local"
    debug: bool = False
    api_prefix: str = "/api/v1"
    docs_enabled: bool = True

    # --- Logging -------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    # Records are handed to a background thread; if it falls behind, drop logs
    # rather than block the event loop or grow memory without bound.
    log_queue_size: int = 10_000
    access_log_skip_paths: frozenset[str] = frozenset({"/health/live", "/health/ready", "/metrics"})

    # --- Server (uvicorn) ----------------------------------------------------
    host: str = "0.0.0.0"  # noqa: S104 - bind all interfaces inside containers
    port: int = 8000
    # One event loop per process; scale with processes, not threads.
    workers: int = Field(default_factory=lambda: os.cpu_count() or 1)
    backlog: int = 2048
    keepalive_timeout: int = 5
    # Sheds load with 503 instead of queueing unbounded work (None = unlimited).
    limit_concurrency: int | None = None
    # Recycle workers after N requests to contain slow memory leaks.
    limit_max_requests: int | None = None
    # Worker threads for sync `def` endpoints / dependencies (anyio default: 40).
    thread_pool_size: int = 40

    # --- Database ------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://app:app@localhost:5432/app"
    db_echo: bool = False
    # Pool is PER WORKER: total = workers * (pool_size + max_overflow).
    # Keep that below Postgres `max_connections`, or put PgBouncer in front.
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_pool_timeout: float = 5.0
    db_pool_recycle: int = 1800
    # One extra round-trip per checkout (measured: ~7% throughput). On: a DB
    # restart/failover causes zero failed requests. Off: the first request per dead
    # connection gets a retryable 503. Turn off if clients retry 503s.
    db_pool_pre_ping: bool = True
    db_statement_timeout_ms: int = 5000
    # Kills sessions that leak an open transaction (they hold locks + a backend).
    db_idle_in_transaction_timeout_ms: int = 10_000

    # --- Redis (optional; cache disabled when unset) ------------------------
    redis_url: str | None = "redis://localhost:6379/0"
    redis_max_connections: int = 100
    redis_socket_timeout: float = 0.5
    cache_ttl_seconds: int = 60

    # --- ClickHouse (optional, read/analytics; disabled when unset) ---------
    clickhouse_url: str | None = None  # e.g. http://default:@localhost:8123/default
    # Bulkhead: max in-flight ClickHouse queries per worker. Excess requests wait
    # up to `clickhouse_queue_timeout` and then get 503, so slow analytics can't
    # consume every coroutine/connection and starve the OLTP endpoints.
    clickhouse_max_concurrency: int = 20
    clickhouse_queue_timeout: float = 1.0
    clickhouse_query_timeout: float = 10.0  # server-side max_execution_time
    clickhouse_connect_timeout: float = 2.0
    clickhouse_max_rows: int = 100_000  # server-side guard against huge results

    # --- Outbound HTTP -------------------------------------------------------
    http_timeout: float = 10.0
    http_max_connections: int = 100
    http_max_keepalive: int = 20

    # --- HTTP ----------------------------------------------------------------
    cors_origins: list[str] = []
    gzip_min_size: int = 1024
    metrics_enabled: bool = True
    health_check_timeout: float = 1.0

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache
def get_settings() -> Settings:
    return Settings()
