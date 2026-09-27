"""Logging must be structured, correlated, and never block the event loop."""

import io
import logging
import queue
import time
from typing import Any

import orjson
from fastapi import FastAPI

from app.core.config import Settings
from app.core.logging import _NonBlockingQueueHandler, flush_logs
from tests.conftest import running_app


class SlowStream(io.StringIO):
    """A stdout that takes 50ms per write (full pipe / stalled log shipper)."""

    def write(self, s: str) -> int:
        time.sleep(0.05)
        return super().write(s)


def _lines(stream: io.StringIO) -> list[dict[str, Any]]:
    flush_logs()
    return [orjson.loads(line) for line in stream.getvalue().splitlines() if line.startswith("{")]


async def test_slow_log_sink_does_not_block_requests(settings: Settings) -> None:
    settings = settings.model_copy(update={"log_level": "INFO"})
    stream = SlowStream()
    async with running_app(settings, log_stream=stream) as (_, client):
        start = time.perf_counter()
        for _ in range(20):
            assert (await client.get("/api/v1/items")).status_code == 200
        elapsed = time.perf_counter() - start

    # If log writes happened on the event loop: 20 x 50ms >= 1s.
    assert elapsed < 0.5, f"requests took {elapsed:.2f}s: logging is blocking the loop"
    access = [line for line in _lines(stream) if line.get("event") == "request"]
    assert len(access) == 20, "records must be delayed, not lost"


async def test_access_log_is_structured_and_correlated(settings: Settings) -> None:
    settings = settings.model_copy(update={"log_level": "INFO"})
    stream = io.StringIO()
    async with running_app(settings, log_stream=stream) as (_, client):
        await client.get("/api/v1/items", headers={"x-request-id": "req-abc"})
        await client.get("/health/live")  # skipped: probe noise

    access = [line for line in _lines(stream) if line.get("event") == "request"]
    assert len(access) == 1
    entry = access[0]
    assert entry["request_id"] == "req-abc"
    assert entry["path"] == "/api/v1/items"
    assert entry["status"] == 200
    assert entry["level"] == "info"
    assert isinstance(entry["duration_ms"], float)
    assert entry["timestamp"].endswith("Z")


async def test_unhandled_error_logged_once_with_traceback(settings: Settings) -> None:
    def setup(app: FastAPI) -> None:
        @app.get("/boom")
        async def boom() -> None:
            raise RuntimeError("secret internal detail")

    settings = settings.model_copy(update={"log_level": "INFO"})
    stream = io.StringIO()
    async with running_app(settings, setup, log_stream=stream) as (_, client):
        resp = await client.get("/boom", headers={"x-request-id": "req-500"})

    # Client: consistent envelope, correlation id, no internals leaked.
    assert resp.status_code == 500
    assert resp.headers["x-request-id"] == "req-500"
    assert resp.json() == {
        "error": {
            "code": "internal_error",
            "message": "Internal server error",
            "request_id": "req-500",
        }
    }
    assert "secret" not in resp.text

    lines = _lines(stream)
    errors = [line for line in lines if line.get("event") == "unhandled_exception"]
    assert len(errors) == 1, "traceback must be logged exactly once"
    (err,) = errors
    assert err["request_id"] == "req-500"
    assert err["level"] == "error"
    # dict_tracebacks: machine-parseable exception, not a text blob.
    assert err["exception"][0]["exc_type"] == "RuntimeError"
    assert err["exception"][0]["frames"]

    access = [line for line in lines if line.get("event") == "request"]
    assert access[0]["status"] == 500
    assert access[0]["level"] == "error"


async def test_untrusted_request_id_is_replaced(client: Any) -> None:
    for bad in ("x" * 200, "has space", "semi;colon"):
        resp = await client.get("/health/live", headers={"x-request-id": bad})
        rid = resp.headers["x-request-id"]
        assert rid != bad
        assert len(rid) == 32


def test_full_log_queue_drops_instead_of_blocking() -> None:
    q: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=1)
    handler = _NonBlockingQueueHandler(q)
    record = logging.makeLogRecord({"msg": "x"})

    start = time.perf_counter()
    for _ in range(3):
        handler.handle(record)

    assert time.perf_counter() - start < 0.01
    assert q.qsize() == 1
    assert handler.dropped == 2
