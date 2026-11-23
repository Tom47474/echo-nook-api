import unittest

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.conversations.models import (
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
)
from app.core.config import get_settings
from app.db.session import DatabaseManager


class ConversationModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        url = get_settings().test_database_url.get_secret_value()
        if not url:
            self.skipTest("TEST_DATABASE_URL is not configured")
        self.manager = DatabaseManager(url, require_test_database=True)

    async def asyncTearDown(self) -> None:
        await self.manager.dispose()

    async def test_messages_are_ordered_and_cascade_with_conversation(self) -> None:
        conversation = Conversation(title="模型测试")
        conversation.messages.extend(
            [
                Message(
                    role=MessageRole.USER.value,
                    content="第一条",
                    status=MessageStatus.COMPLETED.value,
                    sequence=0,
                ),
                Message(
                    role=MessageRole.ASSISTANT.value,
                    content="第二条",
                    status=MessageStatus.COMPLETED.value,
                    sequence=1,
                ),
            ]
        )

        async with self.manager.session() as session:
            session.add(conversation)
            await session.commit()
            conversation_id = conversation.id
            self.assertEqual(conversation_id.version, 7)

            messages = (
                await session.scalars(
                    select(Message)
                    .where(Message.conversation_id == conversation_id)
                    .order_by(Message.sequence)
                )
            ).all()
            self.assertEqual([message.content for message in messages], ["第一条", "第二条"])

            # Execute a database-level delete so this verifies the foreign-key
            # ON DELETE CASCADE rule rather than only the ORM relationship.
            await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            await session.commit()

            remaining = await session.scalars(
                select(Message).where(Message.conversation_id == conversation_id)
            )
            self.assertEqual(remaining.all(), [])

    async def test_message_sequence_is_unique_inside_conversation(self) -> None:
        conversation = Conversation(title="顺序约束测试")
        conversation.messages.append(
            Message(
                role=MessageRole.USER.value,
                content="原消息",
                status=MessageStatus.COMPLETED.value,
                sequence=0,
            )
        )

        async with self.manager.session() as session:
            session.add(conversation)
            await session.commit()
            conversation_id = conversation.id

            session.add(
                Message(
                    conversation_id=conversation_id,
                    role=MessageRole.ASSISTANT.value,
                    content="重复顺序",
                    status=MessageStatus.COMPLETED.value,
                    sequence=0,
                )
            )
            with self.assertRaises(IntegrityError):
                await session.commit()
            await session.rollback()

            await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
            await session.commit()


if __name__ == "__main__":
    unittest.main()
