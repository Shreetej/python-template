from typing import Any

from app.db.clickhouse import ClickHouse

# `{name:Type}` = server-side parameter binding: values never touch the SQL text.
# The WHERE clause matches the table's ORDER BY (item_id, event_time), so
# ClickHouse reads only the granules for this item + time range.
_DAILY_ITEM_STATS = """
SELECT
    toDate(event_time)                        AS day,
    countIf(event_type = 'view')              AS views,
    countIf(event_type = 'purchase')          AS purchases,
    sumIf(amount, event_type = 'purchase')    AS revenue
FROM item_events
WHERE item_id = {item_id:UInt64}
  AND event_time >= now() - toIntervalDay({days:UInt16})
GROUP BY day
ORDER BY day
"""


class AnalyticsRepository:
    def __init__(self, clickhouse: ClickHouse) -> None:
        self.clickhouse = clickhouse

    async def daily_item_stats(self, item_id: int, days: int) -> list[dict[str, Any]]:
        return await self.clickhouse.query(
            _DAILY_ITEM_STATS, parameters={"item_id": item_id, "days": days}
        )
