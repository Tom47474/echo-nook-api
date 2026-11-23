from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.demo import DemoLLMProvider
from app.llm.openai_compatible import OpenAICompatibleProvider


def create_llm_provider(settings: Settings) -> LLMProvider:
    """根据环境配置选择演示 Provider 或真实模型 Provider。"""
    if settings.llm_provider == "demo":
        return DemoLLMProvider()
    return OpenAICompatibleProvider(settings)
