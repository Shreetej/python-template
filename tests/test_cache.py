from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.cache import Cache


class _BrokenRedis:
    async def get(self, *_: object, **__: object) -> bytes:
        raise RedisConnectionError("down")

    async def set(self, *_: object, **__: object) -> None:
        raise RedisConnectionError("down")

    async def delete(self, *_: object) -> None:
        raise RedisConnectionError("down")


async def test_cache_disabled_is_noop() -> None:
    cache = Cache(None, default_ttl=10)
    await cache.set("k", "v")
    assert await cache.get("k") is None


async def test_cache_outage_does_not_raise() -> None:
    cache = Cache(_BrokenRedis(), default_ttl=10)  # type: ignore[arg-type]
    assert await cache.get("k") is None
    await cache.set("k", "v")
    await cache.delete("k")
