# FastAPI High-Performance REST Template

Production-grade async REST API template: FastAPI + uvloop/httptools + SQLAlchemy 2 async (asyncpg) + ClickHouse (reads/analytics) + Redis + structlog + Prometheus, managed with **uv**.

## Quick start

```bash
make install          # uv sync + pre-commit hooks
make up               # api (4 workers) + postgres + redis + clickhouse (5M seeded events) + migrations
curl localhost:8000/health/ready
open http://localhost:8000/docs
make loadtest         # Locust UI at :8089
```

Local dev without Docker for the app itself: `cp .env.example .env`, `docker compose up -d postgres redis clickhouse`, `make migrate`, `make dev`.

If a local Postgres already listens on 5432, set `POSTGRES_HOST_PORT=5433` (and point `APP_DATABASE_URL` at it).

## Stack

| Concern | Choice | Why |
|---|---|---|
| Package/env | **uv** | Fast, lockfile-based installs; same versions in dev, CI, and Docker |
| Server | **uvicorn** + **uvloop** + **httptools** | libuv event loop + C HTTP parser; multi-process workers |
| Framework | **FastAPI** / Pydantic v2 | Validation and JSON serialization run in Rust (pydantic-core) |
| DB | **SQLAlchemy 2 async** + **asyncpg** | Fastest Postgres driver for Python; tuned connection pool |
| Analytics DB | **clickhouse-connect** AsyncClient | Official client; non-blocking aiohttp IO, native binary format, decoding off the event loop |
| Migrations | **Alembic** (async env) | Deterministic constraint naming |
| Cache | **redis-py asyncio** + hiredis | Cache-aside with bounded, blocking pool; fails open |
| Outbound HTTP | **httpx.AsyncClient** (one per worker) | Connection reuse, bounded pool |
| Logging | **structlog** + orjson, queue-based | JSON logs with request-id correlation; writes happen on a background thread |
| Metrics | **prometheus-fastapi-instrumentator** | `/metrics`, multi-process aware |
| Quality | **ruff**, **mypy --strict**, **basedpyright**, **pytest** (+asyncio, cov) | Includes `ASYNC` lint rules that catch blocking calls in async code |
| Load test | **Locust** (FastHttpUser) | |

## Layout

```
src/app/
  main.py              app factory + lifespan (pools created once per worker)
  server.py            uvicorn production entrypoint (`uv run serve`)
  core/                config, logging, ASGI middleware, cache, single-flight, exceptions
  db/                  declarative base, Postgres engine/session, ClickHouse client
  models/ schemas/     SQLAlchemy models / Pydantic DTOs
  repositories/        data access only
  services/            business logic, transactions, caching
  api/deps.py          typed Annotated dependencies
  api/v1/routes/       HTTP handlers (thin)
migrations/            Alembic
tests/                 unit: in-process app + real lifespan, SQLite
tests/integration/     real postgres/redis/clickhouse (auto-skipped if not running)
docker/clickhouse/     ClickHouse schema + seed data + read-only API user
loadtest/              Locust
```

## Performance design

**Concurrency model**
- One event loop per process, `APP_WORKERS` processes (default: CPU count). Scale throughput with processes; the event loop handles concurrency inside each one.
- Every handler is `async def`, so there are no threadpool hops. If you have to call blocking code, use a sync `def` handler (it runs in a threadpool sized by `APP_THREAD_POOL_SIZE`) or `anyio.to_thread.run_sync`. CPU-heavy work belongs in a task queue or a process pool.
- `APP_LIMIT_CONCURRENCY` sheds load with a 503 instead of letting latency climb without bound. `APP_LIMIT_MAX_REQUESTS` recycles workers.

**Serialization**
- Handlers declare return types, so FastAPI serializes straight to JSON bytes in Rust. `ORJSONResponse` is deprecated in current FastAPI and not needed.
- Cache hits skip the DB: `model_validate_json` parses in Rust.

**Database (Postgres)**
- The session dependency uses `Depends(..., scope="function")`, so the connection goes back to the pool **before** the response is sent. FastAPI's default keeps it checked out until the last byte reaches the client, so slow clients can drain the pool (`tests/test_db_session.py` demonstrates both).
- Pool settings are **per worker**: `workers × (pool_size + max_overflow)` must stay below Postgres `max_connections`. Past a few hundred connections, put **PgBouncer** in front and set `statement_cache_size=0` in `db/session.py`.
- Every wait has a deadline:
  - An exhausted pool fails in `pool_timeout` with a 503 + `Retry-After`.
  - `statement_timeout` returns a 504.
  - `idle_in_transaction_session_timeout` kills leaked transactions.
  - The TCP connect has its own timeout.
- `pool_pre_ping` (on by default) means a DB restart or failover causes zero failed requests. It costs about 7% throughput in the measurements below; turn it off if your clients retry 503s.
- Postgres JIT is off: it adds 10–100 ms of compile time to short OLTP queries.
- `expire_on_commit=False` avoids re-SELECTs after commit. Timestamps are set in Python, which saves a RETURNING round-trip.
- **Keyset pagination** (`?after=<cursor>`) costs the same at any depth; OFFSET gets slower the deeper you page.

**ClickHouse (reads / analytics)**
- **Bulkhead:** at most `APP_CLICKHOUSE_MAX_CONCURRENCY` queries in flight per worker. A request waits at most `APP_CLICKHOUSE_QUEUE_TIMEOUT` for a slot, then gets a 503. A slow analytics query can't starve the Postgres endpoints.
- **Limits:** every query carries server-side `max_execution_time` and `max_result_rows`, so ClickHouse cancels runaways and frees their CPU and RAM. A client-side deadline sits slightly above as a backstop.
- **Lazy connect:** a ClickHouse outage doesn't stop the API from booting. After a failed connect there's a 1 s cooldown so requests fail fast instead of each paying a connect timeout.
- **Safety:** queries use server-side parameter binding (`{item_id:UInt64}`), and the DB user is read-only (`readonly=2`).
- **Stateless HTTP:** no session id, so queries on one client run in parallel (ClickHouse serializes queries that share a session).

**Caching and stampedes**
- Cache-aside in Redis. Analytics results are cached for 30 s, which turns a millisecond aggregation into a ~100 µs Redis GET.
- **Single-flight:** when a hot key misses, concurrent requests for it share one query per worker (50 concurrent misses → 1 query in the tests). This stays correct when either the first requester or a later one disconnects.

**Logging**
- The event loop only enqueues a record. JSON rendering and the stdout write run on a background thread. With a stdout that takes 50 ms per write, 20 requests took **2.21 s** with the old synchronous handler and **<0.5 s** now.
- The log queue is bounded: under extreme overload, logs are dropped and counted rather than blocking or growing memory.
- One access line per request, with `request_id`, status and latency. 5xx responses log at error level. Health and metrics probes are skipped.
- Tracebacks are structured (`dict_tracebacks`) and logged **exactly once** per unhandled error.
- Client-supplied `x-request-id` is validated (charset and length) to prevent log and header injection.

**Errors**
- One envelope for every error: `{"error": {"code", "message", "request_id", "details?"}}`. That covers validation errors, 404/405, domain errors, infrastructure errors and unhandled bugs.
- Mapping:
  - Overload or dependency down → **503** + `Retry-After`.
  - Dependency too slow → **504**.
  - Our bugs (SQL syntax, unknown table, read-only violation) → **500**, logged with a traceback.
- Client disconnects are logged as 499, not 500. No internals leak into responses.

**Middleware**
- Custom middleware is pure ASGI, not `BaseHTTPMiddleware`, which adds a task and stream per request and breaks contextvars.
- Uvicorn's access log is off. One structured log line per request comes from `RequestContextMiddleware`, along with `x-request-id` and `server-timing` headers.

**Resilience**
- Redis errors are logged and treated as cache misses, so a Redis outage can't take the API down.
- `/health/live` never touches dependencies. `/health/ready` checks DB, Redis and ClickHouse **concurrently, each with a deadline**, and returns 503 so the load balancer drains the pod.
- Startup and shutdown go through an `AsyncExitStack`: if one resource fails to open, the ones already opened are still closed, in reverse order. The Redis pool is explicitly closed; redis-py doesn't close a user-supplied pool on its own.

## Testing

```bash
make test               # 46 unit tests (in-process, SQLite)
make test-integration   # +11 against real postgres/redis/clickhouse
```

The integration tests cover:
- `statement_timeout` → 504
- pool exhaustion → fast 503s
- killed DB connections → transparent recovery (with pre_ping) or a retryable 503 (without)
- 300 concurrent requests through a 5-connection pool
- Redis down → API still serves
- a real ClickHouse aggregation
- ClickHouse server-side timeout → 504
- read-only user enforcement
- 200 concurrent analytics requests

## Measured throughput

Everything runs on one laptop (Docker Desktop, 4 workers, Locust on the same machine competing for CPU). Each run is 300 users for 30 s, with zero failures in every run.

| Run | req/s | p50 | p99 |
|---|---|---|---|
| Original template (OLTP mix) | 3,219 | 27 ms | ~150–190 ms |
| **Current, same OLTP mix** | **4,161** | **13 ms** | **54 ms** |
| Current, mix + ClickHouse analytics (677 req/s of it) | 4,071 | 14 ms | 53 ms |
| Current, OLTP mix, `pre_ping=false` | 4,325 | 10 ms | — |

A single cached read takes about 0.4 ms server-side (see the `server-timing` header). For real capacity planning, benchmark on production hardware with the load generator on a separate machine.

## Adding a resource

1. `models/foo.py` (import it in `models/__init__.py`)
2. `schemas/foo.py`, `repositories/foo.py`, `services/foo.py`
3. `api/v1/routes/foo.py`, include it in `api/v1/router.py`
4. `make revision m="add foo"` → review → `make migrate`
5. Tests in `tests/`, then `make check`

## Config

All settings are in `src/app/core/config.py`, and each can be overridden with `APP_<NAME>` env vars or `.env` (see `.env.example`).
