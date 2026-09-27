import asyncio

import pytest

from app.core.singleflight import SingleFlight


async def test_concurrent_callers_share_one_execution() -> None:
    flight: SingleFlight[int] = SingleFlight()
    calls = 0

    async def load() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return 42

    results = await asyncio.gather(*(flight.do("k", load) for _ in range(100)))
    assert results == [42] * 100
    assert calls == 1
    assert len(flight) == 0  # no leaked entries


async def test_error_propagates_to_all_and_is_not_cached() -> None:
    flight: SingleFlight[int] = SingleFlight()

    async def fail() -> int:
        await asyncio.sleep(0.01)
        raise ValueError("db down")

    results = await asyncio.gather(
        *(flight.do("k", fail) for _ in range(10)), return_exceptions=True
    )
    assert all(isinstance(r, ValueError) for r in results)

    async def ok() -> int:
        return 1

    assert await flight.do("k", ok) == 1  # next call retries, failure not memoized


async def test_leader_cancellation_does_not_fail_followers() -> None:
    """Leader's client disconnects mid-query: followers must still get a result."""
    flight: SingleFlight[str] = SingleFlight()
    calls = 0

    async def load() -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return "value"

    leader = asyncio.create_task(flight.do("k", load))
    await asyncio.sleep(0.01)
    followers = [asyncio.create_task(flight.do("k", load)) for _ in range(5)]
    await asyncio.sleep(0.01)
    leader.cancel()

    assert await asyncio.gather(*followers) == ["value"] * 5
    with pytest.raises(asyncio.CancelledError):
        await leader
    assert calls == 2  # one follower took over


async def test_follower_cancellation_does_not_cancel_leader() -> None:
    flight: SingleFlight[str] = SingleFlight()

    async def load() -> str:
        await asyncio.sleep(0.05)
        return "value"

    leader = asyncio.create_task(flight.do("k", load))
    await asyncio.sleep(0.01)
    follower = asyncio.create_task(flight.do("k", load))
    await asyncio.sleep(0.01)
    follower.cancel()

    assert await leader == "value"
    with pytest.raises(asyncio.CancelledError):
        await follower


async def test_different_keys_run_in_parallel() -> None:
    flight: SingleFlight[int] = SingleFlight()

    async def load() -> int:
        await asyncio.sleep(0.05)
        return 1

    start = asyncio.get_running_loop().time()
    await asyncio.gather(*(flight.do(f"k{i}", load) for i in range(20)))
    assert asyncio.get_running_loop().time() - start < 0.2
