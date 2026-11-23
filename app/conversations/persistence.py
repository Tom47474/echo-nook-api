import asyncio
import json
from collections.abc import AsyncIterator
from time import monotonic
from uuid import UUID

from sqlalchemy import func, update

from app.conversations.models import Conversation, Message, MessageRole, MessageStatus
from app.conversations.repository import ConversationRepository
from app.conversations.schemas import StreamMessageRequest
from app.conversations.service import encode_event, stream_conversation
from app.db.session import DatabaseManager
from app.llm.base import LLMProvider

DRAFT_FLUSH_CHARS = 200
DRAFT_FLUSH_SECONDS = 1.0
INTERRUPTED_ERROR_CODE = "generation_interrupted"


def decode_sse_event(frame: str) -> tuple[str, dict[str, str]]:
    """解析服务端内部生成的单个 SSE 帧，供持久化层识别状态。"""
    event_name = "message"
    data_lines: list[str] = []
    for line in frame.splitlines():
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    payload = json.loads("\n".join(data_lines)) if data_lines else {}
    return event_name, payload


class AssistantMessageWriter:
    """使用短事务保存助手草稿和终态，不在模型流期间占用连接。"""

    def __init__(self, manager: DatabaseManager, message_id: UUID) -> None:
        self.manager = manager
        self.message_id = message_id

    async def write(
        self,
        content: str,
        status: MessageStatus,
        *,
        error_code: str | None = None,
    ) -> bool:
        """打开独立短 Session，原子更新消息后立即提交并释放连接。"""
        async with self.manager.session() as session:
            updated = await ConversationRepository(session).update_streaming_assistant(
                self.message_id,
                content=content,
                status=status,
                error_code=error_code,
            )
            if updated:
                await session.commit()
            return updated


async def stream_persisted_conversation(
    request: StreamMessageRequest,
    provider: LLMProvider,
    manager: DatabaseManager,
    *,
    user_message_id: UUID,
    assistant_message_id: UUID,
    user_sequence: int,
    assistant_sequence: int,
) -> AsyncIterator[str]:
    """转发模型 SSE，同时保存草稿及 completed/failed/cancelled 终态。"""
    writer = AssistantMessageWriter(manager, assistant_message_id)
    content = ""
    saved_length = 0
    last_saved_at = monotonic()
    terminal_saved = False
    source = stream_conversation(request, provider)

    try:
        # started 事件让后续前端能把本地临时消息与数据库 UUID、sequence 对齐。
        # 它也必须位于 try 内，确保客户端收到 started 后立刻停止仍会执行 finally。
        yield encode_event(
            "message.started",
            {
                "user_message_id": str(user_message_id),
                "assistant_message_id": str(assistant_message_id),
                "user_sequence": str(user_sequence),
                "assistant_sequence": str(assistant_sequence),
            },
        )
        async for frame in source:
            event_name, payload = decode_sse_event(frame)
            if event_name == "message.delta":
                content += payload.get("text", "")
                now = monotonic()
                should_flush = (
                    len(content) - saved_length >= DRAFT_FLUSH_CHARS
                    or now - last_saved_at >= DRAFT_FLUSH_SECONDS
                )
                if should_flush:
                    await writer.write(content, MessageStatus.STREAMING)
                    saved_length = len(content)
                    last_saved_at = now
            elif event_name == "message.completed":
                await writer.write(content, MessageStatus.COMPLETED)
                terminal_saved = True
            elif event_name == "message.failed":
                await writer.write(
                    content,
                    MessageStatus.FAILED,
                    error_code=payload.get("code") or "generation_failed",
                )
                terminal_saved = True
            yield frame
    finally:
        try:
            # 关闭包装流时继续关闭内层模型流，确保上游 HTTP 连接及时释放。
            await source.aclose()
        finally:
            if not terminal_saved:
                # 正常完成和已编码的模型错误都会先写入终态；没有终态说明客户端
                # 主动停止或连接被关闭，此时保留已接收正文并标记 cancelled。
                write_task = asyncio.create_task(
                    writer.write(content, MessageStatus.CANCELLED)
                )
                try:
                    # shield 防止请求取消同时打断最后一次状态落库。
                    await asyncio.shield(write_task)
                except asyncio.CancelledError:
                    await write_task
                    raise


async def recover_interrupted_generations(manager: DatabaseManager) -> int:
    """应用启动时把上次进程遗留的 streaming 消息恢复为 failed。"""
    async with manager.session() as session:
        result = await session.execute(
            update(Message)
            .where(
                Message.role == MessageRole.ASSISTANT.value,
                Message.status == MessageStatus.STREAMING.value,
            )
            .values(
                status=MessageStatus.FAILED.value,
                error_code=INTERRUPTED_ERROR_CODE,
                updated_at=func.now(),
            )
            .returning(Message.conversation_id)
        )
        interrupted_conversation_ids = list(result.scalars())
        conversation_ids = set(interrupted_conversation_ids)
        if conversation_ids:
            await session.execute(
                update(Conversation)
                .where(Conversation.id.in_(conversation_ids))
                .values(updated_at=func.now())
            )
        await session.commit()
        return len(interrupted_conversation_ids)
