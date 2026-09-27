from httpx import AsyncClient


async def test_live(client: AsyncClient) -> None:
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {}}
    assert "x-request-id" in resp.headers


async def test_ready(client: AsyncClient) -> None:
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["checks"] == {"database": "ok"}


async def test_request_id_is_propagated(client: AsyncClient) -> None:
    resp = await client.get("/health/live", headers={"x-request-id": "abc123"})
    assert resp.headers["x-request-id"] == "abc123"
