from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class DailyItemStats(BaseModel):
    day: date
    views: int
    purchases: int
    revenue: Decimal


class ItemStats(BaseModel):
    item_id: int
    days: int
    series: list[DailyItemStats]
