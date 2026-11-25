import asyncio
import json
import unittest

import httpx

from app.conversations.models import Message, MessageRole, MessageStatus
from app.conversations.schemas import StreamMessageRequest
from app.conversations.service import (
    INTERRUPTED_SUFFIX, build_database_history, current_input_tokens,
    estimate_message_tokens, stream_conversation,
)
from app.llm.demo import DemoLLMProvider


class ConversationTests(unittest.TestCase):
    def test_token_budget_keeps_recent_complete_turns(self) -> None:
        messages = [
            Message(role=role, content=content, status=MessageStatus.COMPLETED.value)
            for role, content in (
                (MessageRole.USER.value, "较早问题"),
                (MessageRole.ASSISTANT.value, "较早回答"),
                (MessageRole.USER.value, "最近问题"),
                (MessageRole.ASSISTANT.value, "最近回答"),
            )
        ]
        recent_cost = estimate_message_tokens("最近问题") + estimate_message_tokens("最近回答")
        history = build_database_history(
            messages,
            token_budget=current_input_tokens("新问题") + recent_cost,
            current_message="新问题",
        )
        self.assertEqual([item.content for item in history], ["最近问题", "最近回答"])

    def test_current_question_over_budget_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "当前问题超过模型输入预算"):
            build_database_history([], token_budget=current_input_tokens("短"), current_message="较长问题")

    def test_database_history_keeps_whole_turns_and_marks_interruption(self) -> None:
        messages: list[Message] = []
        for turn in range(11):
            messages.extend(
                [
                    Message(
                        role=MessageRole.USER.value,
                        content=f"问题 {turn}",
                        status=MessageStatus.COMPLETED.value,
                        sequence=turn * 2,
                    ),
                    Message(
                        role=MessageRole.ASSISTANT.value,
                        content=f"回答 {turn}",
                        status=(
                            MessageStatus.FAILED.value
                            if turn == 10
                            else MessageStatus.COMPLETED.value
                        ),
                        sequence=turn * 2 + 1,
                    ),
                ]
            )

        history = build_database_history(messages)

        self.assertEqual(len(history), 20)
        self.assertEqual(history[0].content, "问题 1")
        self.assertEqual(history[-1].content, "回答 10" + INTERRUPTED_SUFFIX)

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
            request = StreamMessageRequest(message="测试")
            return [event async for event in stream_conversation(request, DemoLLMProvider())]

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
            request = StreamMessageRequest(message="测试")
            return [event async for event in stream_conversation(request, UnavailableProvider())]

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
            request = StreamMessageRequest(message="测试")
            return [event async for event in stream_conversation(request, TimeoutProvider())]

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
                    # 客户端断开会关闭响应迭代器；Provider 必须收到关闭信号，
                    # 才能及时退出上游连接的异步上下文。
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
