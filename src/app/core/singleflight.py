"""Per-process request coalescing (a.k.a. single-flight / dogpile protection).

When a hot cache key expires, N concurrent requests would all miss and all run the
same DB/ClickHouse query. With SingleFlight, the first caller (leader) runs it and
the others await the leader's result: N queries become 1 per worker.
"""

import asyncio
from collections.abc import Awaitable, Callable


class SingleFlight[T]:
    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future[T]] = {}

    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:
        while (fut := self._inflight.get(key)) is not None:
            try:
                # shield: a follower being cancelled must not cancel the shared future.
                return await asyncio.shield(fut)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if fut.cancelled() and task is not None and task.cancelling() == 0:
                    continue  # the *leader* was cancelled (client left): retry/take over
                raise

        fut = asyncio.get_running_loop().create_future()
        # Mark the exception as retrieved even when there are no followers.
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._inflight[key] = fut
        try:
            result = await fn()
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except BaseException as exc:
            fut.set_exception(exc)
            raise
        else:
            fut.set_result(result)
            return result
        finally:
            if self._inflight.get(key) is fut:
                del self._inflight[key]

    def __len__(self) -> int:
        return len(self._inflight)
