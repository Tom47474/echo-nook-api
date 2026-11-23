from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.models import Conversation, Message


class ConversationRepository:
    """Keep SQL queries out of HTTP routes and later streaming orchestration."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, title: str) -> Conversation:
        conversation = Conversation(title=title)
        self.session.add(conversation)
        # Flush obtains generated UUID/timestamps while leaving commit ownership
        # with the route's transaction boundary.
        await self.session.flush()
        await self.session.refresh(conversation)
        return conversation

    async def list(self, *, offset: int, limit: int) -> tuple[list[Conversation], int]:
        total = await self.session.scalar(select(func.count()).select_from(Conversation))
        result = await self.session.scalars(
            select(Conversation)
            # UUID is a deterministic tie-breaker when timestamps are equal.
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result), total or 0

    async def exists(self, conversation_id: UUID) -> bool:
        found = await self.session.scalar(
            select(Conversation.id).where(Conversation.id == conversation_id)
        )
        return found is not None

    async def list_messages(
        self,
        conversation_id: UUID,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> tuple[list[Message], bool, int | None]:
        statement = select(Message).where(Message.conversation_id == conversation_id)
        if before_sequence is not None:
            statement = statement.where(Message.sequence < before_sequence)

        # Read newest-first with one extra row to detect another page, then
        # reverse the selected page so clients always render chronological order.
        result = await self.session.scalars(
            statement.order_by(Message.sequence.desc()).limit(limit + 1)
        )
        newest_first = list(result)
        has_more = len(newest_first) > limit
        page = list(reversed(newest_first[:limit]))
        next_before_sequence = page[0].sequence if has_more and page else None
        return page, has_more, next_before_sequence

    async def delete(self, conversation_id: UUID) -> bool:
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation is None:
            return False
        # PostgreSQL performs message deletion through ON DELETE CASCADE.
        await self.session.delete(conversation)
        await self.session.flush()
        return True
