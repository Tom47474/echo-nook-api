import asyncio
from collections.abc import AsyncIterator

from app.conversations.schemas import ChatMessage


class DemoLLMProvider:
    async def stream_reply(self, messages: list[ChatMessage], system_prompt: str) -> AsyncIterator[str]:
        """按固定小片段返回演示文本，用于不消耗模型额度的本地联调。"""
        question = next((item.content for item in reversed(messages) if item.role == "user"), "")
        reply = (
            f"我听到了你说的「{question}」。这是 EchoNook 服务端的演示回复。"
            "接入真实模型后，这里会根据你的问题生成回答。"
        )
        for start in range(0, len(reply), 4):
            await asyncio.sleep(0.04)
            yield reply[start : start + 4]
