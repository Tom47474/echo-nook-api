from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid7

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    PENDING = "pending"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Conversation(Base):
    """会话容器，保存标题、归属预留字段和消息集合。"""

    __tablename__ = "conversations"
    __table_args__ = (
        # 旧版会话可能没有归属；S5-1 起新会话必须由路由写入当前身份并按该字段授权。
        Index("ix_conversations_owner_updated_at", "owner_id", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    owner_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("principals.id", name="fk_conversations_owner_id"), nullable=True)
    title: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
        default="新对话",
        server_default="新对话",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # 摘要仅覆盖已结束的旧回合；序号用于防止与近期原文重复或并发回退。
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    summary_through_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=-1, server_default="-1")
    messages: Mapped[list["Message"]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="Message.sequence",
    )


class Principal(Base):
    """游客或账号的服务端身份；浏览器不能自行指定主键。"""

    __tablename__ = "principals"
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    username: Mapped[str | None] = mapped_column(String(100), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class IdentitySession(Base):
    """只保存随机会话凭证的哈希，泄露数据库也不会直接得到 Cookie。"""

    __tablename__ = "identity_sessions"
    __table_args__ = (Index("ix_identity_sessions_principal_id", "principal_id"),)
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    principal_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("principals.id", ondelete="CASCADE"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class Message(Base):
    """按 sequence 排列的用户或助手消息及其生成状态。"""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        CheckConstraint(
            "status IN ('pending', 'streaming', 'completed', 'failed', 'cancelled')",
            name="ck_messages_status",
        ),
        CheckConstraint("sequence >= 0", name="ck_messages_sequence_non_negative"),
        UniqueConstraint("conversation_id", "sequence", name="uq_messages_conversation_sequence"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    conversation_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=MessageStatus.PENDING.value,
        server_default=MessageStatus.PENDING.value,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    conversation: Mapped[Conversation] = relationship(back_populates="messages")


class ModelCall(Base):
    """记录摘要或主回复的真实模型调用，不保存提示词、正文和密钥。"""

    __tablename__ = "model_calls"
    __table_args__ = (Index("ix_model_calls_conversation_created", "conversation_id", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid7)
    conversation_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False)
    assistant_message_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    finish_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 调用时快照不随游客会话导入而改写；历史 NULL 表示上线身份前无法可靠归因。
    usage_owner_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    usage_owner_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
