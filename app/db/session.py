from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def validate_database_url(url: str, *, require_test_database: bool = False) -> None:
    """校验异步驱动，并阻止测试流程误连非测试数据库。"""
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):
        # 异常中不能包含原始地址，否则可能把数据库密码写入日志。
        raise ValueError("数据库连接地址格式无效") from None
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError("数据库连接必须使用 postgresql+asyncpg 驱动")
    if not parsed.database:
        raise ValueError("数据库连接没有指定数据库名")
    if require_test_database and not parsed.database.endswith("_test"):
        raise ValueError("测试环境只能连接名称以 _test 结尾的数据库")


class DatabaseManager:
    """管理单个 FastAPI 应用使用的异步引擎和 Session 工厂。"""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        require_test_database: bool = False,
    ) -> None:
        validate_database_url(url, require_test_database=require_test_database)
        self.engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def ping(self) -> None:
        """执行最小查询，使 PostgreSQL 不可用时在应用启动阶段立即失败。"""
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """提供一次请求范围的 Session，并在业务异常时回滚未提交事务。"""
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """应用关闭时释放连接池中的数据库连接。"""
        await self.engine.dispose()
