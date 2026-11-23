from collections.abc import AsyncIterator
from typing import Protocol

from app.conversations.schemas import ChatMessage


class LLMProvider(Protocol):
    """所有模型适配器必须实现的流式输出接口。"""

    def stream_reply(self, messages: list[ChatMessage], system_prompt: str) -> AsyncIterator[str]: ...
