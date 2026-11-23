import unittest
from uuid import UUID, uuid4

import httpx
from sqlalchemy import delete, select

from app.conversations.models import (
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
)
from app.core.config import Settings
from app.main import create_app, lifespan


class ConversationApiTests(unittest.IsolatedAsyncioTestCase):
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
        # Cleanup only rows created by this test so a developer's other test data
        # is not removed when the suite runs against the shared local test DB.
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


if __name__ == "__main__":
    unittest.main()
