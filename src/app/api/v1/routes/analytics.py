from typing import Annotated

from fastapi import APIRouter, Path, Query

from app.api.deps import AnalyticsServiceDep
from app.schemas.analytics import ItemStats

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/items/{item_id}/daily")
async def item_daily_stats(
    service: AnalyticsServiceDep,
    item_id: Annotated[int, Path(ge=1)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> ItemStats:
    return await service.item_stats(item_id, days)
