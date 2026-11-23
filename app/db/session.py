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
    """Reject unsupported drivers and unsafe test-database targets."""
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):
        # Do not include the malformed URL because it may contain a password.
        raise ValueError("数据库连接地址格式无效") from None
    if parsed.drivername != "postgresql+asyncpg":
        raise ValueError("数据库连接必须使用 postgresql+asyncpg 驱动")
    if not parsed.database:
        raise ValueError("数据库连接没有指定数据库名")
    if require_test_database and not parsed.database.endswith("_test"):
        raise ValueError("测试环境只能连接名称以 _test 结尾的数据库")


class DatabaseManager:
    """Own the async engine and session factory for one FastAPI application."""

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
        """Fail application startup early when PostgreSQL is unavailable."""
        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield one request-scoped session and roll back failed work."""
        async with self.session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def dispose(self) -> None:
        """Close pooled connections during application shutdown."""
        await self.engine.dispose()
