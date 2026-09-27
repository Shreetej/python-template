"""Pure-ASGI middleware.

`BaseHTTPMiddleware` adds a task + memory stream per request and breaks
contextvars propagation; raw ASGI middleware has near-zero overhead.
"""

import asyncio
import re
import time
import uuid
from collections.abc import Collection

import structlog
from starlette.datastructures import MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "x-request-id"
_REQUEST_ID_BYTES = REQUEST_ID_HEADER.encode()
# Client-supplied ids end up in logs and headers: bound length and charset
# (prevents log injection / header smuggling / unbounded log lines).
_VALID_REQUEST_ID = re.compile(rb"^[A-Za-z0-9._:-]{1,128}$")

access_log = structlog.get_logger("app.access")
error_log = structlog.get_logger("app.error")


def _request_id(scope: Scope) -> str:
    for key, value in scope["headers"]:
        if key == _REQUEST_ID_BYTES:
            if _VALID_REQUEST_ID.match(value):
                return str(value, "ascii")
            break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Request id, one access-log line per request, and the last-resort 500 handler."""

    def __init__(self, app: ASGIApp, skip_paths: Collection[str] = ()) -> None:
        self.app = app
        self.skip_paths = frozenset(skip_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)

        start = time.perf_counter()
        status_code = 500
        response_started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers.append(REQUEST_ID_HEADER, request_id)
                headers.append(
                    "server-timing", f"app;dur={(time.perf_counter() - start) * 1000:.2f}"
                )
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError:
            # Client disconnected / server shutting down: not a server error.
            if not response_started:
                status_code = 499
            raise
        except Exception:  # noqa: BLE001 - last-resort handler, logs traceback
            # Logged once, here, with traceback + request_id. Not re-raised, so the
            # server doesn't log the same traceback a second time.
            error_log.exception("unhandled_exception", method=scope["method"], path=scope["path"])
            status_code = 500
            if not response_started:
                response = JSONResponse(
                    {
                        "error": {
                            "code": "internal_error",
                            "message": "Internal server error",
                            "request_id": request_id,
                        }
                    },
                    status_code=500,
                )
                await response(scope, receive, send_wrapper)
        finally:
            if scope["path"] not in self.skip_paths or status_code >= 500:
                log = access_log.error if status_code >= 500 else access_log.info
                log(
                    "request",
                    method=scope["method"],
                    path=scope["path"],
                    status=status_code,
                    duration_ms=round((time.perf_counter() - start) * 1000, 2),
                    client=scope["client"][0] if scope.get("client") else None,
                )
