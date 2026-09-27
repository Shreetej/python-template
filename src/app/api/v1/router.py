from fastapi import APIRouter

from app.api.v1.routes import analytics, items

api_router = APIRouter()
api_router.include_router(items.router)
api_router.include_router(analytics.router)
