"""Production entrypoint: `uv run serve` / `python -m app`."""

import uvicorn

from app.core.config import get_settings


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        workers=settings.workers,
        loop="uvloop",  # libuv event loop: ~2-4x faster than stock asyncio
        http="httptools",  # C HTTP parser (llhttp)
        backlog=settings.backlog,
        timeout_keep_alive=settings.keepalive_timeout,
        timeout_graceful_shutdown=30,
        limit_concurrency=settings.limit_concurrency,
        limit_max_requests=settings.limit_max_requests,
        access_log=False,  # RequestContextMiddleware logs requests
        log_config=None,  # keep our structlog configuration
        proxy_headers=True,
        forwarded_allow_ips="*",
        server_header=False,
    )


if __name__ == "__main__":
    run()
