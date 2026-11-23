import asyncio
import json
import unittest

import httpx

from app.conversations.schemas import StreamMessageRequest
from app.conversations.service import stream_conversation
from app.llm.demo import DemoLLMProvider


class ConversationTests(unittest.TestCase):
    def test_demo_stream_contains_text_and_completion(self) -> None:
        async def collect() -> list[str]:
            request = StreamMessageRequest(message="你好")
            return [event async for event in stream_conversation(request, DemoLLMProvider())]

        events = asyncio.run(collect())
        self.assertTrue(any("event: message.delta" in event for event in events))
        self.assertIn("你好", "".join(events))
        self.assertEqual(events[-1], "event: message.completed\ndata: {}\n\n")

    def test_event_payload_is_valid_json(self) -> None:
        async def collect() -> list[str]:
            return [event async for event in stream_conversation(StreamMessageRequest(message="测试"), DemoLLMProvider())]

        for event in asyncio.run(collect()):
            json.loads(event.split("data: ", 1)[1].strip())

    def test_unavailable_model_has_actionable_error(self) -> None:
        class UnavailableProvider:
            async def stream_reply(self, messages, system_prompt):
                request = httpx.Request("POST", "https://ark.cn-beijing.volces.com/api/v3/chat/completions")
                response = httpx.Response(404, request=request)
                raise httpx.HTTPStatusError("not found", request=request, response=response)
                yield ""

        async def collect() -> list[str]:
            return [event async for event in stream_conversation(StreamMessageRequest(message="测试"), UnavailableProvider())]

        events = asyncio.run(collect())
        self.assertEqual(len(events), 1)
        self.assertIn("model_not_available", events[0])

    def test_provider_timeout_has_retryable_error_code(self) -> None:
        class TimeoutProvider:
            async def stream_reply(self, messages, system_prompt):
                request = httpx.Request("POST", "https://model.example/chat/completions")
                raise httpx.ReadTimeout("timed out", request=request)
                yield ""

        async def collect() -> list[str]:
            return [event async for event in stream_conversation(StreamMessageRequest(message="测试"), TimeoutProvider())]

        events = asyncio.run(collect())
        self.assertEqual(len(events), 1)
        self.assertIn("provider_timeout", events[0])

    def test_closing_conversation_stream_closes_provider(self) -> None:
        class CancellableProvider:
            def __init__(self) -> None:
                self.closed = False

            async def stream_reply(self, messages, system_prompt):
                try:
                    yield "第一段"
                    await asyncio.sleep(60)
                    yield "不会发送的第二段"
                finally:
                    # A client disconnect closes the response iterator. The
                    # provider must see that close so its upstream connection
                    # can leave its own async context promptly.
                    self.closed = True

        async def consume_then_disconnect() -> bool:
            provider = CancellableProvider()
            stream = stream_conversation(StreamMessageRequest(message="测试取消"), provider)
            await anext(stream)
            await stream.aclose()
            return provider.closed

        self.assertTrue(asyncio.run(consume_then_disconnect()))


if __name__ == "__main__":
    unittest.main()
