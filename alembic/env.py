import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

import app.conversations.models  # 注册模型，供 Alembic 自动比较数据库结构。
from app.core.config import get_settings
from app.db.base import Base
from app.db.session import validate_database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def database_configuration() -> dict[str, str]:
    settings = get_settings()
    url = settings.active_database_url()
    validate_database_url(url, require_test_database=settings.app_env == "test")
    # set_main_option 会把百分号当作插值符；复制配置字典可以保留 URL 编码密码。
    section = dict(config.get_section(config.config_ini_section) or {})
    section["sqlalchemy.url"] = url
    return section


def run_migrations_offline() -> None:
    configuration = database_configuration()
    context.configure(
        url=configuration["sqlalchemy.url"],
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_sync_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        database_configuration(),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    try:
        async with connectable.connect() as connection:
            await connection.run_sync(run_sync_migrations)
    finally:
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
