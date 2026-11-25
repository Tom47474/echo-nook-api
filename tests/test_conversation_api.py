import asyncio
import unittest
from uuid import UUID, uuid4
from unittest.mock import patch

import httpx
from sqlalchemy import delete, select

from app.conversations.models import (
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
)
from app.conversations.service import current_input_tokens
from app.conversations.repository import ConversationRepository
from app.conversations.summary import refresh_summary, summary_source
from app.core.config import Settings
from app.main import create_app, lifespan


class ConversationApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_history_summary_updates_and_failure_falls_back(self) -> None:
        conversation = await self.create_conversation("长对话摘要")
        conversation_id = UUID(conversation["id"])
        messages = [
            Message(role=role, content=f"第 {turn} 回合 {role}",
                    status=MessageStatus.COMPLETED.value, sequence=turn * 2 + offset)
            for turn in range(13)
            for offset, role in enumerate((MessageRole.USER.value, MessageRole.ASSISTANT.value))
        ]
        self.assertEqual(summary_source(messages, -1)[1], 5)

        class SummaryProvider:
            async def stream_reply(self, messages, system_prompt):
                yield "用户讨论了前几个回合。"

        manager = self.app.state.database_manager
        summary, through = await refresh_summary(manager, SummaryProvider(), conversation_id, messages)
        self.assertEqual((summary, through), ("用户讨论了前几个回合。", 5))
        async with manager.session() as session:
            stored = await session.get(Conversation, conversation_id)
            self.assertEqual((stored.summary, stored.summary_through_sequence), (summary, through))

        class FailingProvider:
            async def stream_reply(self, messages, system_prompt):
                raise RuntimeError("摘要生成失败")
                yield ""

        # 增加三个回合使摘要再次达到更新阈值；失败时旧摘要和边界保持有效。
        extended = messages + [
            Message(role=role, content=f"第 {turn} 回合 {role}",
                    status=MessageStatus.COMPLETED.value, sequence=turn * 2 + offset)
            for turn in range(13, 16)
            for offset, role in enumerate((MessageRole.USER.value, MessageRole.ASSISTANT.value))
        ]
        self.assertEqual(await refresh_summary(manager, FailingProvider(), conversation_id, extended), (summary, through))

    async def test_stream_combines_summary_with_recent_original_turns(self) -> None:
        conversation = await self.create_conversation("摘要与原文")
        conversation_id = UUID(conversation["id"])
        async with self.app.state.database_manager.session() as session:
            session.add_all([
                Message(conversation_id=conversation_id, role=role,
                        content=f"第 {turn} 回合 {role}", status=MessageStatus.COMPLETED.value,
                        sequence=turn * 2 + offset)
                for turn in range(13)
                for offset, role in enumerate((MessageRole.USER.value, MessageRole.ASSISTANT.value))
            ])
            await session.commit()

        calls: list[tuple[list, str]] = []

        class RecordingProvider:
            async def stream_reply(self, messages, system_prompt):
                calls.append((messages, system_prompt))
                yield "较早对话摘要" if "压缩" in system_prompt else "正常回答"

        self.app.state.settings = self.app.state.settings.model_copy(update={"llm_provider": "openai_compatible"})
        with patch("app.api.routes.conversations.create_llm_provider", return_value=RecordingProvider()):
            response = await self.client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"message": "当前问题"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(calls), 2)
        self.assertIn("较早对话摘要", calls[1][1])
        self.assertNotIn("第 0 回合", str(calls[1][0]))
        self.assertIn("第 3 回合", str(calls[1][0]))

    async def asyncSetUp(self) -> None:
        settings = Settings(app_env="test")
        if not settings.test_database_url.get_secret_value():
            self.skipTest("TEST_DATABASE_URL is not configured")

        self.app = create_app(settings)
        self.lifespan = lifespan(self.app)
        await self.lifespan.__aenter__()
        transport = httpx.ASGITransport(app=self.app)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")
        self.created_ids: list[UUID] = []

    async def asyncTearDown(self) -> None:
        # 只清理本测试创建的数据，避免运行测试时删除开发者保留的其他测试数据。
        if self.created_ids:
            async with self.app.state.database_manager.session() as session:
                await session.execute(
                    delete(Conversation).where(Conversation.id.in_(self.created_ids))
                )
                await session.commit()
        await self.client.aclose()
        await self.lifespan.__aexit__(None, None, None)

    async def create_conversation(self, title: str) -> dict[str, object]:
        response = await self.client.post("/api/v1/conversations", json={"title": title})
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        self.created_ids.append(UUID(payload["id"]))
        return payload

    async def test_create_list_and_delete_conversation(self) -> None:
        older = await self.create_conversation("  较早的会话  ")
        newer = await self.create_conversation("较新的会话")
        self.assertEqual(older["title"], "较早的会话")

        response = await self.client.get("/api/v1/conversations", params={"limit": 100})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        ids = [item["id"] for item in payload["items"]]
        self.assertLess(ids.index(newer["id"]), ids.index(older["id"]))
        self.assertGreaterEqual(payload["total"], 2)

        response = await self.client.delete(f"/api/v1/conversations/{older['id']}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")

        response = await self.client.delete(f"/api/v1/conversations/{older['id']}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "conversation_not_found")

    async def test_get_conversation_by_id_for_direct_link(self) -> None:
        conversation = await self.create_conversation("直达链接")
        response = await self.client.get(f"/api/v1/conversations/{conversation['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "直达链接")

        missing = await self.client.get(f"/api/v1/conversations/{uuid4()}")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "conversation_not_found")

    async def test_message_query_uses_sequence_cursor(self) -> None:
        conversation = await self.create_conversation("分页会话")
        conversation_id = UUID(conversation["id"])
        async with self.app.state.database_manager.session() as session:
            session.add_all(
                [
                    Message(
                        conversation_id=conversation_id,
                        role=(
                            MessageRole.USER.value
                            if sequence % 2 == 0
                            else MessageRole.ASSISTANT.value
                        ),
                        content=f"消息 {sequence}",
                        status=MessageStatus.COMPLETED.value,
                        sequence=sequence,
                    )
                    for sequence in range(5)
                ]
            )
            await session.commit()

        first = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            params={"limit": 2},
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual([item["sequence"] for item in first.json()["items"]], [3, 4])
        self.assertTrue(first.json()["has_more"])
        self.assertEqual(first.json()["next_before_sequence"], 3)

        second = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            params={"limit": 2, "before_sequence": 3},
        )
        self.assertEqual([item["sequence"] for item in second.json()["items"]], [1, 2])
        self.assertEqual(second.json()["next_before_sequence"], 1)

        last = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            params={"limit": 2, "before_sequence": 1},
        )
        self.assertEqual([item["sequence"] for item in last.json()["items"]], [0])
        self.assertFalse(last.json()["has_more"])
        self.assertIsNone(last.json()["next_before_sequence"])

    async def test_delete_cascades_messages_and_missing_conversation_is_404(self) -> None:
        conversation = await self.create_conversation("删除会话")
        conversation_id = UUID(conversation["id"])
        message = Message(
            conversation_id=conversation_id,
            role=MessageRole.USER.value,
            content="将被级联删除",
            status=MessageStatus.COMPLETED.value,
            sequence=0,
        )
        async with self.app.state.database_manager.session() as session:
            session.add(message)
            await session.commit()
            message_id = message.id

        response = await self.client.delete(f"/api/v1/conversations/{conversation_id}")
        self.assertEqual(response.status_code, 204)
        async with self.app.state.database_manager.session() as session:
            self.assertIsNone(await session.scalar(select(Message).where(Message.id == message_id)))

        missing = await self.client.get(
            f"/api/v1/conversations/{uuid4()}/messages"
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"]["code"], "conversation_not_found")

    async def test_pagination_parameters_are_validated(self) -> None:
        response = await self.client.get(
            "/api/v1/conversations",
            params={"offset": -1, "limit": 101},
        )
        self.assertEqual(response.status_code, 422)

    async def test_conversation_stream_saves_user_and_uses_database_history(self) -> None:
        conversation = await self.create_conversation("服务端历史")
        conversation_id = UUID(conversation["id"])
        async with self.app.state.database_manager.session() as session:
            session.add_all(
                [
                    Message(
                        conversation_id=conversation_id,
                        role=MessageRole.USER.value,
                        content="数据库中的问题",
                        status=MessageStatus.COMPLETED.value,
                        sequence=0,
                    ),
                    Message(
                        conversation_id=conversation_id,
                        role=MessageRole.ASSISTANT.value,
                        content="数据库中的回答",
                        status=MessageStatus.COMPLETED.value,
                        sequence=1,
                    ),
                ]
            )
            await session.commit()

        captured_messages = []
        checked_out_connections = []
        captured_environments = []
        engine = self.app.state.database_manager.engine

        class CapturingProvider:
            async def stream_reply(self, messages, system_prompt):
                captured_messages.extend(messages)
                # Provider 开始工作时，历史查询事务应已释放数据库连接。
                checked_out_connections.append(engine.pool.checkedout())
                yield "新的回答"

        def create_provider(settings):
            captured_environments.append(settings.app_env)
            return CapturingProvider()

        with patch(
            "app.api.routes.conversations.create_llm_provider",
            side_effect=create_provider,
        ):
            response = await self.client.post(
                f"/api/v1/conversations/{conversation_id}/messages:stream",
                json={"message": "本次问题"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("event: message.delta", response.text)
        self.assertIn("event: message.completed", response.text)
        self.assertEqual(checked_out_connections, [0])
        self.assertEqual(captured_environments, ["test"])
        self.assertEqual(
            [(item.role, item.content) for item in captured_messages],
            [
                ("user", "数据库中的问题"),
                ("assistant", "数据库中的回答"),
                ("user", "本次问题"),
            ],
        )

        stored = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )
        self.assertEqual(
            [(item["sequence"], item["content"]) for item in stored.json()["items"]],
            [
                (0, "数据库中的问题"),
                (1, "数据库中的回答"),
                (2, "本次问题"),
                (3, "新的回答"),
            ],
        )
        self.assertEqual(
            [item["status"] for item in stored.json()["items"][-2:]],
            ["completed", "completed"],
        )

    async def test_conversation_stream_rejects_client_history_and_missing_id(self) -> None:
        conversation = await self.create_conversation("拒绝客户端历史")
        response = await self.client.post(
            f"/api/v1/conversations/{conversation['id']}/messages:stream",
            json={"message": "新问题", "history": [{"role": "user", "content": "伪造历史"}]},
        )
        self.assertEqual(response.status_code, 422)

        response = await self.client.post(
            f"/api/v1/conversations/{uuid4()}/messages:stream",
            json={"message": "不存在的会话"},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "conversation_not_found")

    async def test_over_budget_question_does_not_create_messages(self) -> None:
        conversation = await self.create_conversation("超长输入")
        settings = self.app.state.settings
        budget = current_input_tokens("这条问题会超限") - 1
        self.app.state.settings = settings.model_copy(update={
            "llm_context_window_tokens": settings.llm_max_output_tokens
            + settings.llm_token_safety_margin + budget,
        })
        response = await self.client.post(
            f"/api/v1/conversations/{conversation['id']}/messages:stream",
            json={"message": "这条问题会超限"},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "input_budget_exceeded")
        stored = await self.client.get(f"/api/v1/conversations/{conversation['id']}/messages")
        self.assertEqual(stored.json()["items"], [])

    async def test_model_history_keeps_empty_final_attempt_for_turn_selection(self) -> None:
        conversation = await self.create_conversation("多次助手尝试")
        conversation_id = UUID(conversation["id"])
        async with self.app.state.database_manager.session() as session:
            session.add_all([
                Message(conversation_id=conversation_id, role=MessageRole.USER.value,
                        content="问题", status=MessageStatus.COMPLETED.value, sequence=0),
                Message(conversation_id=conversation_id, role=MessageRole.ASSISTANT.value,
                        content="旧回答", status=MessageStatus.COMPLETED.value, sequence=1),
                Message(conversation_id=conversation_id, role=MessageRole.ASSISTANT.value,
                        content="", status=MessageStatus.FAILED.value, sequence=2),
            ])
            await session.commit()
            history = await ConversationRepository(session).load_model_history(
                conversation_id, before_sequence=3,
            )
        self.assertEqual([item.sequence for item in history], [0, 1, 2])

    async def test_concurrent_user_messages_receive_unique_sequences(self) -> None:
        conversation = await self.create_conversation("并发顺序")
        conversation_id = UUID(conversation["id"])

        class ImmediateProvider:
            async def stream_reply(self, messages, system_prompt):
                yield "完成"

        with patch(
            "app.api.routes.conversations.create_llm_provider",
            return_value=ImmediateProvider(),
        ):
            responses = await asyncio.gather(
                self.client.post(
                    f"/api/v1/conversations/{conversation_id}/messages:stream",
                    json={"message": "并发问题 A"},
                ),
                self.client.post(
                    f"/api/v1/conversations/{conversation_id}/messages:stream",
                    json={"message": "并发问题 B"},
                ),
            )

        self.assertEqual([response.status_code for response in responses], [200, 200])
        stored = await self.client.get(
            f"/api/v1/conversations/{conversation_id}/messages"
        )
        self.assertEqual(
            [item["sequence"] for item in stored.json()["items"]],
            [0, 1, 2, 3],
        )
        self.assertEqual(
            [item["role"] for item in stored.json()["items"]],
            ["user", "assistant", "user", "assistant"],
        )


if __name__ == "__main__":
    unittest.main()
