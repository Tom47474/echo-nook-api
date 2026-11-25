import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from app.conversations.schemas import ChatMessage
from app.core.config import Settings
from app.llm.openai_compatible import OpenAICompatibleProvider


class ArkProviderTests(unittest.TestCase):
    def test_missing_usage_stays_unknown(self) -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, text=(
                'data: {"choices":[{"delta":{"content":"回答"},"finish_reason":null}],'
                '"usage":{"prompt_tokens":2}}\n\n'
                'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                'data: [DONE]\n\n'
            ),
        ))
        original_client = httpx.AsyncClient
        provider = OpenAICompatibleProvider(Settings(
            _env_file=None, llm_api_key="test-only-key", llm_model="test-model-id",
        ))

        async def collect() -> list[str]:
            with patch(
                "app.llm.openai_compatible.httpx.AsyncClient",
                side_effect=lambda **kwargs: original_client(transport=transport, **kwargs),
            ):
                return [part async for part in provider.stream_reply([], "system")]

        self.assertEqual(asyncio.run(collect()), ["回答"])
        self.assertIsNone(provider.last_call_metrics.prompt_tokens)
        self.assertIsNone(provider.last_call_metrics.completion_tokens)
        self.assertIsNone(provider.last_call_metrics.total_tokens)

    def test_stream_extracts_answer_and_skips_reasoning(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            body = "\n\n".join(
                [
                    'data: {"choices":[{"delta":{"reasoning_content":"thinking"}}]}',
                    'data: {"choices":[{"delta":{"content":"你好"}}]}',
                    'data: {"choices":[{"delta":{"content":"，世界"}}]}',
                    'data: {"model":"actual-model","choices":[{"delta":{},"finish_reason":"stop"}]}',
                    'data: {"choices":[],"usage":{"prompt_tokens":12,"completion_tokens":7,"total_tokens":19}}',
                    "data: [DONE]",
                ]
            )
            return httpx.Response(200, text=body)

        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        settings = Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key="test-only-key",
            llm_model="test-model-id",
        )

        provider = OpenAICompatibleProvider(settings)

        async def collect() -> list[str]:
            with patch(
                "app.llm.openai_compatible.httpx.AsyncClient",
                side_effect=lambda **kwargs: original_client(transport=transport, **kwargs),
            ):
                messages = [ChatMessage(role="user", content="你好")]
                return [
                    part
                    async for part in provider.stream_reply(messages, "be helpful")
                ]

        self.assertEqual(asyncio.run(collect()), ["你好", "，世界"])
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].url.path, "/api/v3/chat/completions")
        payload = json.loads(captured[0].content)
        self.assertEqual(payload["model"], "test-model-id")
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(payload["max_tokens"], settings.llm_max_output_tokens)
        self.assertEqual(payload["stream_options"], {"include_usage": True})
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "be helpful"})
        self.assertEqual(provider.last_call_metrics.model, "actual-model")
        self.assertEqual(provider.last_call_metrics.finish_reason, "stop")
        self.assertEqual(provider.last_call_metrics.prompt_tokens, 12)
        self.assertEqual(provider.last_call_metrics.completion_tokens, 7)
        self.assertEqual(provider.last_call_metrics.total_tokens, 19)
        self.assertIsNotNone(provider.last_call_metrics.duration_ms)

    def test_missing_key_fails_before_request(self) -> None:
        settings = Settings(_env_file=None, llm_api_key="")

        async def collect() -> None:
            provider = OpenAICompatibleProvider(settings)
            async for _ in provider.stream_reply([], "system"):
                pass

        with self.assertRaises(ValueError):
            asyncio.run(collect())

    def test_missing_model_fails_before_request(self) -> None:
        settings = Settings(_env_file=None, llm_api_key="test-only-key", llm_model="")

        async def collect() -> None:
            provider = OpenAICompatibleProvider(settings)
            async for _ in provider.stream_reply([], "system"):
                pass

        with self.assertRaisesRegex(ValueError, "LLM_MODEL"):
            asyncio.run(collect())


if __name__ == "__main__":
    unittest.main()
