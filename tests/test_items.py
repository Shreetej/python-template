import asyncio

from httpx import AsyncClient

API = "/api/v1/items"


async def _create(client: AsyncClient, name: str, price: str = "9.99") -> dict[str, object]:
    resp = await client.post(API, json={"name": name, "price": price})
    assert resp.status_code == 201, resp.text
    body: dict[str, object] = resp.json()
    return body


async def test_crud_roundtrip(client: AsyncClient) -> None:
    created = await _create(client, "widget")
    item_id = created["id"]

    resp = await client.get(f"{API}/{item_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "widget"

    resp = await client.patch(f"{API}/{item_id}", json={"price": "19.50"})
    assert resp.status_code == 200
    assert resp.json()["price"] == "19.50"

    resp = await client.delete(f"{API}/{item_id}")
    assert resp.status_code == 204

    resp = await client.get(f"{API}/{item_id}")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_duplicate_name_conflicts(client: AsyncClient) -> None:
    await _create(client, "dup")
    resp = await client.post(API, json={"name": "dup", "price": "1.00"})
    assert resp.status_code == 409


async def test_validation(client: AsyncClient) -> None:
    resp = await client.post(API, json={"name": "", "price": "-1"})
    assert resp.status_code == 422
    resp = await client.post(API, json={"name": "x", "price": "1", "unknown": True})
    assert resp.status_code == 422


async def test_keyset_pagination(client: AsyncClient) -> None:
    for i in range(5):
        await _create(client, f"item-{i}")

    page1 = (await client.get(API, params={"limit": 2})).json()
    assert [i["name"] for i in page1["items"]] == ["item-0", "item-1"]
    assert page1["next_cursor"] is not None

    page2 = (await client.get(API, params={"limit": 2, "after": page1["next_cursor"]})).json()
    assert [i["name"] for i in page2["items"]] == ["item-2", "item-3"]

    page3 = (await client.get(API, params={"limit": 2, "after": page2["next_cursor"]})).json()
    assert [i["name"] for i in page3["items"]] == ["item-4"]
    assert page3["next_cursor"] is None


async def test_concurrent_requests(client: AsyncClient) -> None:
    created = await _create(client, "hot")
    responses = await asyncio.gather(*(client.get(f"{API}/{created['id']}") for _ in range(50)))
    assert all(r.status_code == 200 for r in responses)
