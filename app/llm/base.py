from collections.abc import AsyncIterator
from typing import Protocol

from app.conversations.schemas import ChatMessage


class LLMProvider(Protocol):
    def stream_reply(self, messages: list[ChatMessage], system_prompt: str) -> AsyncIterator[str]: ...
