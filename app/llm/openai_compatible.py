"""火山方舟 Chat Completions 流式适配器。

供应商请求和响应解析集中在此处，避免 API 路由依赖方舟协议细节。
"""

import json
from collections.abc import AsyncIterator
from time import monotonic

import httpx

from app.conversations.schemas import ChatMessage
from app.core.config import Settings
from app.llm.metrics import LLMCallMetrics


class OpenAICompatibleProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_call_metrics: LLMCallMetrics | None = None

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

        started_at = monotonic()
        metrics = LLMCallMetrics(model=self.settings.llm_model)
        self.last_call_metrics = metrics

        url = self.settings.llm_api_base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.settings.llm_model,
            "stream": True,
            # 方舟的用量可能只在 choices=[] 的尾块返回，必须显式请求。
            "stream_options": {"include_usage": True},
            "reasoning_effort": self.settings.llm_reasoning_effort,
            # 显式限制输出，和业务层预留的 token 空间保持一致。
            "max_tokens": self.settings.llm_max_output_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                *[{"role": item.role, "content": item.content} for item in messages],
            ],
        }
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=20.0, pool=10.0)
        emitted_text = False
        # 上游响应必须处于异步上下文内，取消时才能及时关闭连接，
        # 避免模型请求在客户端离开后继续占用资源。
        try:
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
                        if isinstance(event.get("model"), str):
                            metrics.model = event["model"]
                        choices = event.get("choices")
                        # 只把尾块或带结束原因的终块用量当作最终值；中途累计值不能冒充总量。
                        is_final_chunk = not choices or any(
                            isinstance(choice.get("finish_reason"), str) for choice in choices
                        )
                        usage = event.get("usage")
                        if is_final_chunk and isinstance(usage, dict):
                            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                value = usage.get(key)
                                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                                    setattr(metrics, key, value)
                        if not choices:
                            continue
                        choice = choices[0]
                        if isinstance(choice.get("finish_reason"), str):
                            metrics.finish_reason = choice["finish_reason"]
                        content = choice.get("delta", {}).get("content")
                        # Kimi 可能先返回 reasoning_content；界面只展示最终正文 content。
                        if isinstance(content, str) and content:
                            emitted_text = True
                            yield content
        finally:
            # 包含上游连接关闭时间；取消或异常时也能得到已耗费的时长。
            metrics.duration_ms = max(0, round((monotonic() - started_at) * 1000))

        if not emitted_text:
            raise RuntimeError("模型未返回可展示的正文")
