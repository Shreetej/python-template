"""Best-effort Redis cache. A cache outage degrades latency, never availability."""

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

logger = structlog.get_logger(__name__)


class Cache:
    def __init__(self, redis: Redis | None, default_ttl: int, namespace: str = "app") -> None:
        self._redis = redis
        self._ttl = default_ttl
        self._ns = namespace

    def _key(self, key: str) -> str:
        return f"{self._ns}:{key}"

    async def get(self, key: str) -> bytes | str | None:
        if self._redis is None:
            return None
        try:
            return await self._redis.get(self._key(key))
        except RedisError as exc:
            logger.warning("cache_get_failed", key=key, error=str(exc))
            return None

    async def set(self, key: str, value: bytes | str, ttl: int | None = None) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(self._key(key), value, ex=ttl or self._ttl)
        except RedisError as exc:
            logger.warning("cache_set_failed", key=key, error=str(exc))

    async def delete(self, *keys: str) -> None:
        if self._redis is None or not keys:
            return
        try:
            await self._redis.delete(*(self._key(k) for k in keys))
        except RedisError as exc:
            logger.warning("cache_delete_failed", keys=keys, error=str(exc))
