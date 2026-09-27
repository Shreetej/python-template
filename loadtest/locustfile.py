"""Load test. `make loadtest` then open http://localhost:8089.

Headless: uv run locust -f loadtest/locustfile.py --host http://localhost:8000 \
    --headless -u 500 -r 50 -t 1m
OLTP only (no ClickHouse): add --exclude-tags analytics
"""

import random

from locust import between, events, tag, task
from locust.contrib.fasthttp import FastHttpUser
from locust.env import Environment

API = "/api/v1/items"
ITEM_IDS: list[int] = []


@events.test_start.add_listener
def seed(environment: Environment, **_: object) -> None:
    """Create a working set of items so read traffic hits real rows."""
    import httpx  # noqa: PLC0415

    host = environment.host or "http://localhost:8000"
    with httpx.Client(base_url=host) as client:
        for i in range(100):
            resp = client.post(API, json={"name": f"load-{random.random()}-{i}", "price": "9.99"})  # noqa: S311
            if resp.status_code == 201:
                ITEM_IDS.append(resp.json()["id"])


class ApiUser(FastHttpUser):  # gevent + geventhttpclient: far more RPS per core
    wait_time = between(0.01, 0.1)

    @task(10)
    def get_item(self) -> None:
        if ITEM_IDS:
            self.client.get(f"{API}/{random.choice(ITEM_IDS)}", name=f"{API}/:id")  # noqa: S311

    @task(3)
    def list_items(self) -> None:
        self.client.get(API, params={"limit": 20})

    @task(1)
    def create_item(self) -> None:
        self.client.post(API, json={"name": f"u-{random.random()}", "price": "1.00"})  # noqa: S311

    @tag("analytics")
    @task(3)
    def item_stats(self) -> None:
        item_id = random.randint(1, 1000)  # noqa: S311
        self.client.get(
            f"/api/v1/analytics/items/{item_id}/daily",
            params={"days": 30},
            name="/api/v1/analytics/items/:id/daily",
        )

    @task(1)
    def health(self) -> None:
        self.client.get("/health/live")
