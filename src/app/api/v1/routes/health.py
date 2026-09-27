import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import structlog
from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import Settings
from app.db.clickhouse import ClickHouse

router = APIRouter(prefix="/health", tags=["health"])
logger = structlog.get_logger(__name__)


class HealthStatus(BaseModel):
    status: str
    checks: dict[str, str] = {}


@router.get("/live")
async def live() -> HealthStatus:
    """Liveness: the process is up. Never touches dependencies."""
    return HealthStatus(status="ok")


async def _check(name: str, probe: Callable[[], Awaitable[object]], deadline_s: float) -> str:
    try:
        async with asyncio.timeout(deadline_s):
            await probe()
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        logger.warning("readiness_check_failed", check=name, error=repr(exc))
        return "error"
    return "ok"


@router.get("/ready")
async def ready(request: Request, response: Response) -> HealthStatus:
    """Readiness: dependencies reachable. Take the pod out of rotation otherwise.

    Checks run concurrently, each with a deadline, so a hung dependency can't
    make the probe itself hang (and a busy DB pool can't block it for pool_timeout).
    """
    state = request.app.state
    settings = cast("Settings", state.settings)
    engine = cast("AsyncEngine", state.engine)

    async def db() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))

    probes: dict[str, Callable[[], Awaitable[object]]] = {"database": db}
    if (redis := cast("Redis | None", state.redis)) is not None:
        probes["redis"] = redis.ping
    if (clickhouse := cast("ClickHouse | None", state.clickhouse)) is not None:

        async def ch() -> None:
            if not await clickhouse.ping():  # returns False instead of raising
                raise ConnectionError("ClickHouse ping failed")

        probes["clickhouse"] = ch

    results = await asyncio.gather(
        *(_check(name, probe, settings.health_check_timeout) for name, probe in probes.items())
    )
    checks = dict(zip(probes, results, strict=True))

    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthStatus(status="ok" if healthy else "degraded", checks=checks)
