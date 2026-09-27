from typing import Any, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import Cache
from app.core.exceptions import ConflictError, NotFoundError
from app.core.singleflight import SingleFlight
from app.models.item import Item
from app.repositories.item import ItemRepository
from app.schemas.item import ItemCreate, ItemPage, ItemRead, ItemUpdate


class ItemService:
    """Business logic + transaction boundaries + cache-aside reads."""

    def __init__(self, session: AsyncSession, cache: Cache, flight: SingleFlight[Any]) -> None:
        self.session = session
        self.repo = ItemRepository(session)
        self.cache = cache
        self.flight = flight

    @staticmethod
    def _key(item_id: int) -> str:
        return f"item:{item_id}"

    async def get(self, item_id: int) -> ItemRead:
        if cached := await self.cache.get(self._key(item_id)):
            # Rust-side JSON parse + validation; the DB is never touched.
            return ItemRead.model_validate_json(cached)

        # Cache miss: concurrent requests for the same id share ONE DB query.
        async def load() -> ItemRead:
            item = await self._get_or_404(item_id)
            result = ItemRead.model_validate(item)
            await self.session.rollback()  # release the connection before the cache write
            await self.cache.set(self._key(item_id), result.model_dump_json())
            return result

        return cast("ItemRead", await self.flight.do(self._key(item_id), load))

    async def list(self, after: int | None, limit: int) -> ItemPage:
        # Fetch one extra row to know whether another page exists.
        rows = await self.repo.list_after(after, limit + 1)
        items = [ItemRead.model_validate(r) for r in rows[:limit]]
        next_cursor = items[-1].id if len(rows) > limit else None
        return ItemPage(items=items, next_cursor=next_cursor)

    async def create(self, data: ItemCreate) -> ItemRead:
        try:
            item = await self.repo.add(**data.model_dump())
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError(f"Item '{data.name}' already exists") from exc
        return ItemRead.model_validate(item)

    async def update(self, item_id: int, data: ItemUpdate) -> ItemRead:
        item = await self._get_or_404(item_id)
        try:
            await self.repo.update(item, **data.model_dump(exclude_unset=True))
            await self.session.commit()
        except IntegrityError as exc:
            await self.session.rollback()
            raise ConflictError("Item name already exists") from exc
        await self.cache.delete(self._key(item_id))
        return ItemRead.model_validate(item)

    async def delete(self, item_id: int) -> None:
        item = await self._get_or_404(item_id)
        await self.repo.delete(item)
        await self.session.commit()
        await self.cache.delete(self._key(item_id))

    async def _get_or_404(self, item_id: int) -> Item:
        item = await self.repo.get(item_id)
        if item is None:
            raise NotFoundError(f"Item {item_id} not found")
        return item
