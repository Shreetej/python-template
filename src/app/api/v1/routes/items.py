from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.deps import ItemServiceDep
from app.schemas.item import ItemCreate, ItemPage, ItemRead, ItemUpdate

router = APIRouter(prefix="/items", tags=["items"])

# Every handler is `async def` and declares its return type: FastAPI then
# serializes via pydantic-core (Rust) directly to JSON bytes.


@router.get("")
async def list_items(
    service: ItemServiceDep,
    after: Annotated[int | None, Query(description="Cursor from previous page")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ItemPage:
    return await service.list(after=after, limit=limit)


@router.get("/{item_id}")
async def get_item(item_id: int, service: ItemServiceDep) -> ItemRead:
    return await service.get(item_id)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_item(data: ItemCreate, service: ItemServiceDep) -> ItemRead:
    return await service.create(data)


@router.patch("/{item_id}")
async def update_item(item_id: int, data: ItemUpdate, service: ItemServiceDep) -> ItemRead:
    return await service.update(item_id, data)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(item_id: int, service: ItemServiceDep) -> None:
    await service.delete(item_id)
