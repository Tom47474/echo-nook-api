"""Volcengine Ark Chat Completions streaming adapter.

This provider keeps Ark-specific request and response details out of the API layer.
"""

import json
from collections.abc import AsyncIterator

import httpx

from app.conversations.schemas import ChatMessage
from app.core.config import Settings


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream_reply(self, messages: list[ChatMessage], system_prompt: str) -> AsyncIterator[str]:
        if not self.settings.llm_api_key:
            raise ValueError("LLM_API_KEY 未配置")
        if not self.settings.llm_model.strip():
            raise ValueError("LLM_MODEL 未配置")

        url = self.settings.llm_api_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "stream": True,
            "reasoning_effort": self.settings.llm_reasoning_effort,
            "messages": [
                {"role": "system", "content": system_prompt},
                *[{"role": item.role, "content": item.content} for item in messages],
            ],
        }
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=20.0, pool=10.0)
        emitted_text = False
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {self.settings.llm_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    if not data:
                        continue
                    event = json.loads(data)
                    if not event.get("choices"):
                        continue
                    content = event["choices"][0].get("delta", {}).get("content")
                    # Kimi may stream reasoning_content first. Only the final answer is shown.
                    if isinstance(content, str) and content:
                        emitted_text = True
                        yield content

        if not emitted_text:
            raise RuntimeError("模型未返回可展示的正文")
