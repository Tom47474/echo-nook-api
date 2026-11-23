from collections.abc import AsyncIterator

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import DatabaseManager


def get_database_manager(request: Request) -> DatabaseManager:
    """取得 FastAPI 生命周期创建的应用级数据库管理器。"""
    return request.app.state.database_manager


async def get_db_session(
    manager: DatabaseManager = Depends(get_database_manager),
) -> AsyncIterator[AsyncSession]:
    """向接口注入独立且支持事务的异步数据库 Session。"""
    async with manager.session() as session:
        yield session
