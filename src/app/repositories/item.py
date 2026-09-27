from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.item import Item


class ItemRepository:
    """Data access only: no HTTP, no caching, no transaction boundaries."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, item_id: int) -> Item | None:
        return await self.session.get(Item, item_id)

    async def list_after(self, after: int | None, limit: int) -> Sequence[Item]:
        stmt = select(Item).order_by(Item.id).limit(limit)
        if after is not None:
            stmt = stmt.where(Item.id > after)
        return (await self.session.scalars(stmt)).all()

    async def add(self, **values: object) -> Item:
        item = Item(**values)
        self.session.add(item)
        await self.session.flush()
        return item

    async def update(self, item: Item, **values: object) -> Item:
        for key, value in values.items():
            setattr(item, key, value)
        await self.session.flush()
        return item

    async def delete(self, item: Item) -> None:
        await self.session.delete(item)
        await self.session.flush()
