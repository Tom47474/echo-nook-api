from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.models import (
    Conversation,
    Message,
    MessageRole,
    MessageStatus,
)


class ConversationRepository:
    """封装会话领域的数据库查询，避免路由和流式编排直接拼接 SQL。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, title: str) -> Conversation:
        """创建会话但不提交事务，由调用接口统一决定提交时机。"""
        conversation = Conversation(title=title)
        self.session.add(conversation)
        # flush 用于取得 UUID 和服务端时间，commit 仍由接口事务边界负责。
        await self.session.flush()
        await self.session.refresh(conversation)
        return conversation

    async def list(self, *, offset: int, limit: int) -> tuple[list[Conversation], int]:
        """按最近更新时间倒序返回会话列表和未分页的总数。"""
        total = await self.session.scalar(select(func.count()).select_from(Conversation))
        result = await self.session.scalars(
            select(Conversation)
            # 时间相同时使用 UUID 作为稳定排序键，防止分页结果漂移。
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .offset(offset)
            .limit(limit)
        )
        return list(result), total or 0

    async def exists(self, conversation_id: UUID) -> bool:
        """只查询主键判断会话是否存在，避免加载无关字段。"""
        found = await self.session.scalar(
            select(Conversation.id).where(Conversation.id == conversation_id)
        )
        return found is not None

    async def get(self, conversation_id: UUID) -> Conversation | None:
        """按主键读取单个会话，供直接打开 URL 时验证并取得标题。"""
        return await self.session.get(Conversation, conversation_id)

    async def list_messages(
        self,
        conversation_id: UUID,
        *,
        before_sequence: int | None,
        limit: int,
    ) -> tuple[list[Message], bool, int | None]:
        """使用 sequence 游标读取最新一页，并返回下一页信息。"""
        statement = select(Message).where(Message.conversation_id == conversation_id)
        if before_sequence is not None:
            statement = statement.where(Message.sequence < before_sequence)

        # 倒序多取一条用于判断下一页，再反转成前端需要的时间正序。
        result = await self.session.scalars(
            statement.order_by(Message.sequence.desc()).limit(limit + 1)
        )
        newest_first = list(result)
        has_more = len(newest_first) > limit
        page = list(reversed(newest_first[:limit]))
        next_before_sequence = page[0].sequence if has_more and page else None
        return page, has_more, next_before_sequence

    async def delete(self, conversation_id: UUID) -> bool:
        """删除会话；不存在返回 False，消息由数据库外键级联删除。"""
        conversation = await self.session.get(Conversation, conversation_id)
        if conversation is None:
            return False
        # 消息删除交给 PostgreSQL 的 ON DELETE CASCADE，避免逐条删除。
        await self.session.delete(conversation)
        await self.session.flush()
        return True

    async def start_turn(
        self,
        conversation_id: UUID,
        content: str,
    ) -> tuple[Message, Message] | None:
        """在同一行锁事务中创建用户消息和助手流式占位消息。"""
        conversation = await self.session.scalar(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
        )
        if conversation is None:
            return None

        latest_sequence = await self.session.scalar(
            select(func.max(Message.sequence)).where(
                Message.conversation_id == conversation_id
            )
        )
        first_sequence = 0 if latest_sequence is None else latest_sequence + 1
        user_message = Message(
            conversation_id=conversation_id,
            role=MessageRole.USER.value,
            content=content,
            status=MessageStatus.COMPLETED.value,
            sequence=first_sequence,
        )
        assistant_message = Message(
            conversation_id=conversation_id,
            role=MessageRole.ASSISTANT.value,
            content="",
            status=MessageStatus.STREAMING.value,
            sequence=first_sequence + 1,
        )
        # 两条消息在释放行锁前一起 flush，防止并发回合交叉占用 sequence。
        self.session.add_all((user_message, assistant_message))
        # 新消息应使会话回到列表顶部；更新时间由数据库生成。
        conversation.updated_at = func.now()
        await self.session.flush()
        await self.session.refresh(user_message)
        await self.session.refresh(assistant_message)
        return user_message, assistant_message

    async def update_streaming_assistant(
        self,
        message_id: UUID,
        *,
        content: str,
        status: MessageStatus,
        error_code: str | None = None,
    ) -> bool:
        """更新仍处于 streaming 的助手消息，终态消息不会被重复覆盖。"""
        message = await self.session.scalar(
            select(Message)
            .where(Message.id == message_id)
            .with_for_update()
        )
        if message is None or message.status != MessageStatus.STREAMING.value:
            return False

        message.content = content
        message.status = status.value
        message.error_code = error_code
        message.updated_at = func.now()
        # 草稿和终态变化都会刷新会话时间，使活跃会话保持在列表顶部。
        conversation = await self.session.get(Conversation, message.conversation_id)
        if conversation is not None:
            conversation.updated_at = func.now()
        await self.session.flush()
        return True

    async def load_model_history(
        self,
        conversation_id: UUID,
        *,
        before_sequence: int,
        scan_limit: int = 100,
    ) -> list[Message]:
        """读取当前问题之前可进入模型上下文的最近消息。"""
        result = await self.session.scalars(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.sequence < before_sequence,
                Message.status.in_(
                    (
                        MessageStatus.COMPLETED.value,
                        MessageStatus.FAILED.value,
                        MessageStatus.CANCELLED.value,
                    )
                ),
            )
            .order_by(Message.sequence.desc())
            .limit(scan_limit)
        )
        # SQL 为倒序取最近数据，返回业务层前恢复为时间正序。
        return list(reversed(list(result)))
