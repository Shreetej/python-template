-- Example analytics table. ORDER BY matches the hot query pattern
-- (filter by item_id, range on event_time), so reads touch few granules.
CREATE TABLE IF NOT EXISTS default.item_events
(
    event_time  DateTime CODEC(Delta, ZSTD),
    item_id     UInt64,
    event_type  LowCardinality(String),
    amount      Decimal(12, 2)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(event_time)
ORDER BY (item_id, event_time);

-- 5M synthetic events over the last 60 days for 1000 items.
INSERT INTO default.item_events
SELECT
    now() - toIntervalSecond(rand() % (86400 * 60)),
    rand(1) % 1000 + 1,
    ['view', 'view', 'view', 'cart', 'purchase'][rand(2) % 5 + 1],
    toDecimal64((rand(3) % 10000) / 100, 2)
FROM numbers(5000000);

-- Read-only API user: the app can't write or drop anything even if a query is
-- built badly. readonly=2 still lets the client set per-query limits.
CREATE USER IF NOT EXISTS api IDENTIFIED WITH plaintext_password BY 'api'
    SETTINGS readonly = 2;
GRANT SELECT ON default.* TO api;
