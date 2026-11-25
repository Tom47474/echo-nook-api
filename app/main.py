from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router
from app.conversations.persistence import recover_interrupted_generations
from app.core.config import Settings, get_settings
from app.db.session import DatabaseManager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时验证 PostgreSQL，关闭时释放应用持有的连接池。"""
    settings: Settings = app.state.settings
    manager = DatabaseManager(
        settings.active_database_url(),
        echo=settings.database_echo,
        require_test_database=settings.app_env == "test",
    )
    app.state.database_manager = manager
    try:
        await manager.ping()
        # 当前部署为单进程模型；启动时修复上次异常退出遗留的生成中消息。
        await recover_interrupted_generations(manager)
        yield
    finally:
        await manager.dispose()


def create_app(settings: Settings | None = None) -> FastAPI:
    """创建 FastAPI 应用，注册跨域、生命周期和全部业务路由。"""
    settings = settings or get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.frontend_origin],
        # 浏览器以 HttpOnly Cookie 携带游客或登录会话，跨端口请求必须允许凭证。
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type"],
    )
    app.include_router(router)
    return app


app = create_app()
