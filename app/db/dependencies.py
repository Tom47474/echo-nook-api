from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import DatabaseManager


def get_database_manager(request: Request) -> DatabaseManager:
    """Read the manager created by the FastAPI lifespan."""
    return request.app.state.database_manager


async def get_db_session(
    manager: DatabaseManager = Depends(get_database_manager),
) -> AsyncIterator[AsyncSession]:
    """Provide one transaction-capable session to a request handler."""
    async with manager.session() as session:
        yield session
