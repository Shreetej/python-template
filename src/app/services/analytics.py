from typing import Any, cast

from app.core.cache import Cache
from app.core.singleflight import SingleFlight
from app.db.clickhouse import ClickHouse
from app.repositories.analytics import AnalyticsRepository
from app.schemas.analytics import DailyItemStats, ItemStats

# Analytics tolerate slight staleness; caching them is the biggest throughput win
# (a ClickHouse aggregation costs ms-to-seconds, a Redis GET ~100us).
_STATS_TTL_SECONDS = 30


class AnalyticsService:
    def __init__(self, clickhouse: ClickHouse, cache: Cache, flight: SingleFlight[Any]) -> None:
        self.repo = AnalyticsRepository(clickhouse)
        self.cache = cache
        self.flight = flight

    async def item_stats(self, item_id: int, days: int) -> ItemStats:
        key = f"stats:item:{item_id}:{days}"
        if cached := await self.cache.get(key):
            return ItemStats.model_validate_json(cached)

        async def load() -> ItemStats:
            rows = await self.repo.daily_item_stats(item_id, days)
            stats = ItemStats(
                item_id=item_id,
                days=days,
                series=[DailyItemStats.model_validate(r) for r in rows],
            )
            await self.cache.set(key, stats.model_dump_json(), ttl=_STATS_TTL_SECONDS)
            return stats

        return cast("ItemStats", await self.flight.do(key, load))
