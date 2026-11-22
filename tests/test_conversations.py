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


if __name__ == "__main__":
    unittest.main()
