"""长对话摘要更新：只压缩已终结的旧回合，失败时保留近期原文。"""

import asyncio
from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select, update

from app.conversations.models import Conversation, Message, MessageRole
from app.conversations.call_records import record_model_call
from app.conversations.schemas import ChatMessage
from app.conversations.service import build_database_history
from app.db.session import DatabaseManager
from app.llm.base import LLMProvider

RECENT_TURNS = 10
SUMMARY_BATCH_TURNS = 3
SUMMARY_MAX_CHARS = 2000
SUMMARY_PROMPT = (
    "请把较早对话压缩为准确的中文摘要，保留用户偏好、已确认事实、未解决的问题和关键结论。"
    "不要编造事实；失败或取消的回答不得写成确定结论。只返回摘要正文，最多 2000 字。"
)


def summary_source(messages: Sequence[Message], through_sequence: int) -> tuple[list[Message], int] | None:
    """留出最近十个回合，仅选择摘要边界之后最早的三个完整旧回合。"""
    remaining = [item for item in messages if item.sequence > through_sequence]
    user_positions = [index for index, item in enumerate(remaining) if item.role == MessageRole.USER.value]
    if len(user_positions) <= RECENT_TURNS:
        return None
    old_turn_count = min(len(user_positions) - RECENT_TURNS, SUMMARY_BATCH_TURNS)
    cutoff = user_positions[old_turn_count]
    source = remaining[user_positions[0]:cutoff]
    return (source, source[-1].sequence) if source else None


async def refresh_summary(
    manager: DatabaseManager,
    provider: LLMProvider,
    conversation_id: UUID,
    messages: Sequence[Message],
    *,
    usage_owner_id: UUID | None = None,
    usage_owner_kind: str | None = None,
    timeout_seconds: float | None = None,
) -> tuple[str, int]:
    """按需生成摘要；模型或数据库更新失败时返回已保存版本，不阻断主回复。"""
    async with manager.session() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            return "", -1
        previous, through, epoch = conversation.summary, conversation.summary_through_sequence, conversation.content_epoch

    source = summary_source(messages, through)
    if source is None:
        return previous, through
    old_messages, new_through = source
    # 摘要请求独立于主回复；限制输入，避免摘要本身挤占模型上下文。
    canonical = build_database_history(old_messages)
    lines = [f"{item.role}：{item.content[:250]}" for item in canonical]
    prompt = ("既有摘要：\n" + previous + "\n\n新覆盖的旧消息：\n" + "\n".join(lines))[:4000]
    chunks: list[str] = []
    call_status = "failed"
    generation_failed = False
    stream = provider.stream_reply([ChatMessage(role="user", content=prompt)], SUMMARY_PROMPT)
    try:
        # 摘要超时只触发回退，不得阻断本次主回复。
        async with asyncio.timeout(timeout_seconds):
            async for chunk in stream:
                chunks.append(chunk)
                if sum(map(len, chunks)) > SUMMARY_MAX_CHARS:
                    break
    except Exception:
        generation_failed = True
    finally:
        # 提前达到长度上限时也要释放上游 SSE 连接。
        await stream.aclose()
    updated_summary = "".join(chunks).strip()
    if not generation_failed and updated_summary and len(updated_summary) <= SUMMARY_MAX_CHARS:
        call_status = "completed"
    try:
        await record_model_call(
            manager, provider, conversation_id, purpose="summary", status=call_status,
            usage_owner_id=usage_owner_id, usage_owner_kind=usage_owner_kind,
        )
    except Exception:
        # 调用记录的写入失败不应阻断主回复或摘要回退。
        pass
    if generation_failed or not updated_summary or len(updated_summary) > SUMMARY_MAX_CHARS:
        return previous, through

    # 只允许从读取时的边界向前推进；并发请求不能用旧摘要覆盖较新摘要。
    async with manager.session() as session:
        result = await session.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id, Conversation.summary_through_sequence == through,
                   Conversation.content_epoch == epoch)
            .values(summary=updated_summary, summary_through_sequence=new_through)
        )
        if result.rowcount:
            await session.commit()
            return updated_summary, new_through
        current = await session.scalar(select(Conversation).where(Conversation.id == conversation_id))
        return (current.summary, current.summary_through_sequence) if current else ("", -1)
