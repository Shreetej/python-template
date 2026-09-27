from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ItemBase(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    price: Decimal = Field(ge=0, max_digits=12, decimal_places=2)


class ItemCreate(ItemBase):
    model_config = ConfigDict(extra="forbid")


class ItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10_000)
    price: Decimal | None = Field(default=None, ge=0, max_digits=12, decimal_places=2)


class ItemRead(ItemBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    updated_at: datetime


class ItemPage(BaseModel):
    items: list[ItemRead]
    # Keyset cursor: pass as `?after=` to fetch the next page. O(log n) at any depth,
    # unlike OFFSET which scans and discards every skipped row.
    next_cursor: int | None
