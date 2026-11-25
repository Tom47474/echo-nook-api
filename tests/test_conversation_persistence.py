import asyncio
import unittest
from uuid import UUID
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import delete, select

from app.conversations.models import Conversation, Message, MessageStatus, ModelCall
from app.conversations.persistence import (
    INTERRUPTED_ERROR_CODE,
    recover_interrupted_generations,
    stream_persisted_conversation,
)
from app.conversations.repository import ConversationRepository
from app.conversations.schemas import StreamMessageRequest
from app.core.config import get_settings
from app.db.session import DatabaseManager
from app.llm.metrics import LLMCallMetrics


class ConversationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        url = get_settings().test_database_url.get_secret_value()
        if not url:
            self.skipTest("TEST_DATABASE_URL is not configured")
        self.manager = DatabaseManager(url, require_test_database=True)
        self.conversation_ids: list[UUID] = []

    async def asyncTearDown(self) -> None:
        if self.conversation_ids:
            async with self.manager.session() as session:
                await session.execute(
                    delete(Conversation).where(
                        Conversation.id.in_(self.conversation_ids)
                    )
                )
                await session.commit()
        await self.manager.dispose()

    async def create_turn(self) -> tuple[UUID, UUID, int, int]:
        """创建测试会话和一个生成中回合，返回脱离 Session 的标量。"""
        async with self.manager.session() as session:
            repository = ConversationRepository(session)
            conversation = await repository.create("持久化测试")
            await session.flush()
            turn = await repository.start_turn(conversation.id, "测试问题")
            assert turn is not None
            user_message, assistant_message = turn
            await session.commit()
            self.conversation_ids.append(conversation.id)
            return (
                user_message.id,
                assistant_message.id,
                user_message.sequence,
                assistant_message.sequence,
            )

    async def read_message(self, message_id: UUID) -> Message:
        """用新 Session 读取消息，确保断言针对已提交数据库状态。"""
        async with self.manager.session() as session:
            message = await session.get(Message, message_id)
            assert message is not None
            return message

    async def test_provider_failure_saves_partial_text_and_error_code(self) -> None:
        user_id, assistant_id, user_sequence, assistant_sequence = (
            await self.create_turn()
        )

        class FailingProvider:
            async def stream_reply(self, messages, system_prompt):
                yield "失败前的部分正文"
                request = httpx.Request("POST", "https://model.example/chat")
                raise httpx.ReadTimeout("timeout", request=request)

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"),
            FailingProvider(),
            self.manager,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            user_sequence=user_sequence,
            assistant_sequence=assistant_sequence,
        )
        events = [event async for event in stream]

        self.assertTrue(events[0].startswith("event: message.started"))
        self.assertIn("event: message.failed", events[-1])
        assistant = await self.read_message(assistant_id)
        self.assertEqual(assistant.status, MessageStatus.FAILED.value)
        self.assertEqual(assistant.content, "失败前的部分正文")
        self.assertEqual(assistant.error_code, "provider_timeout")

    async def test_failed_call_records_known_fields_without_inventing_usage(self) -> None:
        user_id, assistant_id, user_sequence, assistant_sequence = await self.create_turn()
        conversation_id = (await self.read_message(assistant_id)).conversation_id

        class FailingProvider:
            last_call_metrics = None

            async def stream_reply(self, messages, system_prompt):
                self.last_call_metrics = LLMCallMetrics(model="known-model", duration_ms=12)
                request = httpx.Request("POST", "https://model.example/chat")
                raise httpx.ReadTimeout("timeout", request=request)
                yield ""

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"), FailingProvider(), self.manager,
            user_message_id=user_id, assistant_message_id=assistant_id,
            user_sequence=user_sequence, assistant_sequence=assistant_sequence,
            conversation_id=conversation_id,
        )
        _ = [event async for event in stream]
        async with self.manager.session() as session:
            records = list((await session.scalars(select(ModelCall).where(
                ModelCall.conversation_id == conversation_id,
            ))).all())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "failed")
        self.assertEqual(records[0].model, "known-model")
        self.assertIsNone(records[0].prompt_tokens)
        self.assertIsNone(records[0].finish_reason)

    async def test_completed_call_is_recorded_before_terminal_event_reaches_browser(self) -> None:
        """浏览器收到 completed 后立即断开时，usage 也必须已经提交。"""
        user_id, assistant_id, user_sequence, assistant_sequence = await self.create_turn()
        conversation_id = (await self.read_message(assistant_id)).conversation_id

        class CompletedProvider:
            last_call_metrics = None

            async def stream_reply(self, messages, system_prompt):
                self.last_call_metrics = LLMCallMetrics(
                    model="usage-model", duration_ms=25,
                    prompt_tokens=12, completion_tokens=8, total_tokens=20,
                    finish_reason="stop",
                )
                yield "完整回答"

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"), CompletedProvider(), self.manager,
            user_message_id=user_id, assistant_message_id=assistant_id,
            user_sequence=user_sequence, assistant_sequence=assistant_sequence,
            conversation_id=conversation_id,
        )
        while True:
            event = await anext(stream)
            if "event: message.completed" in event:
                break

        # 此时尚未请求生成器的下一个事件，模拟浏览器读到 completed 就取消响应体。
        async with self.manager.session() as session:
            record = await session.scalar(select(ModelCall).where(
                ModelCall.assistant_message_id == assistant_id,
            ))
        self.assertIsNotNone(record)
        self.assertEqual((record.prompt_tokens, record.completion_tokens, record.total_tokens), (12, 8, 20))
        await stream.aclose()

    async def test_call_record_failure_is_logged_without_changing_completed_reply(self) -> None:
        """内部观测写入失败应有日志，但不能把用户已完成的回答改成失败。"""
        user_id, assistant_id, user_sequence, assistant_sequence = await self.create_turn()
        conversation_id = (await self.read_message(assistant_id)).conversation_id

        class CompletedProvider:
            last_call_metrics = LLMCallMetrics(model="usage-model", total_tokens=20)

            async def stream_reply(self, messages, system_prompt):
                yield "完整回答"

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"), CompletedProvider(), self.manager,
            user_message_id=user_id, assistant_message_id=assistant_id,
            user_sequence=user_sequence, assistant_sequence=assistant_sequence,
            conversation_id=conversation_id,
        )
        with patch(
            "app.conversations.persistence.record_model_call",
            new=AsyncMock(side_effect=RuntimeError("测试写入失败")),
        ), self.assertLogs("app.conversations.persistence", level="ERROR") as logs:
            events = [event async for event in stream]

        self.assertIn("event: message.completed", events[-1])
        self.assertIn("保存模型调用记录失败", "\n".join(logs.output))
        assistant = await self.read_message(assistant_id)
        self.assertEqual(assistant.status, MessageStatus.COMPLETED.value)

    async def test_closing_stream_saves_cancelled_partial_text(self) -> None:
        user_id, assistant_id, user_sequence, assistant_sequence = (
            await self.create_turn()
        )

        class SlowProvider:
            async def stream_reply(self, messages, system_prompt):
                yield "用户停止前的正文"
                await asyncio.sleep(60)
                yield "不会返回"

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"),
            SlowProvider(),
            self.manager,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            user_sequence=user_sequence,
            assistant_sequence=assistant_sequence,
        )
        await anext(stream)  # message.started
        await anext(stream)  # message.delta
        await stream.aclose()

        assistant = await self.read_message(assistant_id)
        self.assertEqual(assistant.status, MessageStatus.CANCELLED.value)
        self.assertEqual(assistant.content, "用户停止前的正文")
        self.assertIsNone(assistant.error_code)

    async def test_cancelled_call_records_status_with_unknown_usage(self) -> None:
        user_id, assistant_id, user_sequence, assistant_sequence = await self.create_turn()
        conversation_id = (await self.read_message(assistant_id)).conversation_id

        class SlowProvider:
            last_call_metrics = None

            async def stream_reply(self, messages, system_prompt):
                self.last_call_metrics = LLMCallMetrics(model="known-model", duration_ms=8)
                yield "部分正文"
                await asyncio.sleep(60)

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"), SlowProvider(), self.manager,
            user_message_id=user_id, assistant_message_id=assistant_id,
            user_sequence=user_sequence, assistant_sequence=assistant_sequence,
            conversation_id=conversation_id,
        )
        await anext(stream)
        await anext(stream)
        await stream.aclose()
        async with self.manager.session() as session:
            record = await session.scalar(select(ModelCall).where(ModelCall.conversation_id == conversation_id))
        self.assertEqual(record.status, "cancelled")
        self.assertIsNone(record.total_tokens)

    async def test_task_cancellation_still_records_cancelled_call(self) -> None:
        """ASGI 请求任务被取消时，独立清理任务仍保存调用记录。"""
        user_id, assistant_id, user_sequence, assistant_sequence = await self.create_turn()
        conversation_id = (await self.read_message(assistant_id)).conversation_id
        delta_seen = asyncio.Event()

        class SlowProvider:
            last_call_metrics = None

            async def stream_reply(self, messages, system_prompt):
                self.last_call_metrics = LLMCallMetrics(model="cancel-model", duration_ms=9)
                yield "部分正文"
                await asyncio.sleep(60)

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"), SlowProvider(), self.manager,
            user_message_id=user_id, assistant_message_id=assistant_id,
            user_sequence=user_sequence, assistant_sequence=assistant_sequence,
            conversation_id=conversation_id,
        )

        async def consume() -> None:
            async for event in stream:
                if "event: message.delta" in event:
                    delta_seen.set()

        task = asyncio.create_task(consume())
        await delta_seen.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        async with self.manager.session() as session:
            record = await session.scalar(select(ModelCall).where(
                ModelCall.assistant_message_id == assistant_id,
            ))
        self.assertIsNotNone(record)
        self.assertEqual(record.status, "cancelled")

    async def test_closing_after_started_still_marks_cancelled(self) -> None:
        user_id, assistant_id, user_sequence, assistant_sequence = (
            await self.create_turn()
        )

        class WaitingProvider:
            async def stream_reply(self, messages, system_prompt):
                await asyncio.sleep(60)
                yield "不会返回"

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"),
            WaitingProvider(),
            self.manager,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            user_sequence=user_sequence,
            assistant_sequence=assistant_sequence,
        )
        await anext(stream)
        await stream.aclose()

        assistant = await self.read_message(assistant_id)
        self.assertEqual(assistant.status, MessageStatus.CANCELLED.value)
        self.assertEqual(assistant.content, "")

    async def test_large_delta_is_checkpointed_while_streaming(self) -> None:
        user_id, assistant_id, user_sequence, assistant_sequence = (
            await self.create_turn()
        )

        class LargeDeltaProvider:
            async def stream_reply(self, messages, system_prompt):
                yield "草" * 200
                await asyncio.sleep(60)

        stream = stream_persisted_conversation(
            StreamMessageRequest(message="测试问题"),
            LargeDeltaProvider(),
            self.manager,
            user_message_id=user_id,
            assistant_message_id=assistant_id,
            user_sequence=user_sequence,
            assistant_sequence=assistant_sequence,
        )
        await anext(stream)
        await anext(stream)

        draft = await self.read_message(assistant_id)
        self.assertEqual(draft.status, MessageStatus.STREAMING.value)
        self.assertEqual(draft.content, "草" * 200)
        await stream.aclose()

    async def test_startup_recovery_marks_streaming_message_failed(self) -> None:
        _, assistant_id, _, _ = await self.create_turn()

        recovered = await recover_interrupted_generations(self.manager)

        self.assertGreaterEqual(recovered, 1)
        assistant = await self.read_message(assistant_id)
        self.assertEqual(assistant.status, MessageStatus.FAILED.value)
        self.assertEqual(assistant.error_code, INTERRUPTED_ERROR_CODE)


if __name__ == "__main__":
    unittest.main()
