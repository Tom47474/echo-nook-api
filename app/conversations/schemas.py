from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.conversations.models import MessageStatus


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class StreamMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)


class ConversationStreamRequest(BaseModel):
    """新流式接口只接收本次输入，历史必须由服务端数据库提供。"""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)


class ConversationCreate(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=200)

    @field_validator("title")
    @classmethod
    def title_must_contain_text(cls, value: str) -> str:
        # 在 HTTP 边界统一清理标题，避免客户端空格差异进入数据库。
        value = value.strip()
        if not value:
            raise ValueError("标题不能为空")
        return value


class ConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationList(BaseModel):
    items: list[ConversationRead]
    total: int
    offset: int
    limit: int


class MessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: Literal["user", "assistant"]
    content: str
    status: MessageStatus
    sequence: int
    error_code: str | None
    created_at: datetime
    updated_at: datetime


class MessagePage(BaseModel):
    items: list[MessageRead]
    limit: int
    has_more: bool
    next_before_sequence: int | None
