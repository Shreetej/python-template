"""Structured, non-blocking logging.

- structlog for app logs; stdlib loggers (uvicorn, sqlalchemy, ...) routed through
  the same pipeline so every line is one JSON object with the request_id.
- The event loop only enqueues a LogRecord (~1us). Rendering + the blocking write
  to stdout happen on a background thread (QueueHandler -> QueueListener). A slow
  stdout (full pipe, stalled log shipper) can no longer stall request handling.
- The queue is bounded: under extreme overload logs are dropped (and counted)
  instead of blocking or exhausting memory.
"""

import atexit
import logging
import queue
import sys
from collections.abc import Callable
from logging.handlers import QueueHandler, QueueListener
from typing import TextIO

import orjson
import structlog

from app.core.config import Settings

_listener: QueueListener | None = None


def _orjson_dumps(
    obj: object, default: Callable[[object], object] | None = None, **_: object
) -> str:
    return orjson.dumps(obj, default=default).decode()


class _NonBlockingQueueHandler(QueueHandler):
    """Enqueue raw records; never block, never format on the event loop."""

    def __init__(self, q: "queue.Queue[logging.LogRecord]") -> None:
        super().__init__(q)
        self.dropped = 0

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Default prepare() formats on the caller's thread; defer to the listener.
        return record

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.dropped += 1


def configure_logging(settings: Settings, stream: TextIO | None = None) -> None:
    global _listener  # noqa: PLW0603 - process-wide logging pipeline

    shared: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,  # request_id etc.
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: structlog.types.Processor
    if settings.log_json:
        shared.append(structlog.processors.dict_tracebacks)  # structured, not a blob
        renderer = structlog.processors.JSONRenderer(serializer=_orjson_dumps)
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,  # cheap early exit for disabled levels
            *shared,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    output = logging.StreamHandler(stream or sys.stdout)
    output.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )

    if _listener is not None:  # reconfiguration (tests, reload): flush the old pipeline
        _listener.stop()
    log_queue: queue.Queue[logging.LogRecord] = queue.Queue(maxsize=settings.log_queue_size)
    _listener = QueueListener(log_queue, output, respect_handler_level=True)
    _listener.start()

    root = logging.getLogger()
    root.handlers = [_NonBlockingQueueHandler(log_queue)]
    root.setLevel(settings.log_level)

    # Access logs are emitted by our own middleware (with request_id + latency).
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True


def flush_logs() -> None:
    """Block until every queued record is written (tests, before process exit)."""
    if _listener is not None:
        _listener.stop()  # drains the queue
        _listener.start()


def dropped_log_records() -> int:
    return sum(getattr(h, "dropped", 0) for h in logging.getLogger().handlers)


@atexit.register
def _flush_on_exit() -> None:
    if _listener is not None:
        _listener.stop()
