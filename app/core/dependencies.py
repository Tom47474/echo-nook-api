from fastapi import Request

from app.core.config import Settings
from app.core.limits import ConversationLimitManager


def get_app_settings(request: Request) -> Settings:
    """向接口提供当前 FastAPI 实例实际使用的环境配置。"""
    # 从 app.state 读取可保证测试创建的应用和全局默认应用不会混用配置。
    return request.app.state.settings


def get_conversation_limit_manager(request: Request) -> ConversationLimitManager:
    """取得应用生命周期内共享的限额器，保证并发状态不会按请求重建。"""
    return request.app.state.conversation_limit_manager
