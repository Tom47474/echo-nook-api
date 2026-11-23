"""火山方舟 Chat Completions 流式适配器。

供应商请求和响应解析集中在此处，避免 API 路由依赖方舟协议细节。
"""

import json
from collections.abc import AsyncIterator

import httpx

from app.conversations.schemas import ChatMessage
from app.core.config import Settings


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def stream_reply(
        self,
        messages: list[ChatMessage],
        system_prompt: str,
    ) -> AsyncIterator[str]:
        """调用方舟流式接口，只向业务层产出最终回答正文。"""
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
        # 上游响应必须处于异步上下文内，取消时才能及时关闭连接，
        # 避免模型请求在客户端离开后继续占用资源。
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
                    # Kimi 可能先返回 reasoning_content；界面只展示最终正文 content。
                    if isinstance(content, str) and content:
                        emitted_text = True
                        yield content

        if not emitted_text:
            raise RuntimeError("模型未返回可展示的正文")
