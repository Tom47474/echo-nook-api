from fastapi import APIRouter

from app.api.routes import conversations, health, identity

router = APIRouter()
router.include_router(health.router)
router.include_router(conversations.router, prefix="/api/v1")
router.include_router(identity.router, prefix="/api/v1")
