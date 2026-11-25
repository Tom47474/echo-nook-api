from fastapi import Request

from app.core.config import Settings


def get_app_settings(request: Request) -> Settings:
    """向接口提供当前 FastAPI 实例实际使用的环境配置。"""
    # 从 app.state 读取可保证测试创建的应用和全局默认应用不会混用配置。
    return request.app.state.settings
