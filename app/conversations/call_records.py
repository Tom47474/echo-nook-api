"""将模型适配器实际提供的元数据写入数据库。"""

from uuid import UUID

from app.conversations.models import ModelCall
from app.db.session import DatabaseManager
from app.llm.base import LLMProvider
from app.llm.metrics import LLMCallMetrics


async def record_model_call(
    manager: DatabaseManager,
    provider: LLMProvider,
    conversation_id: UUID,
    *,
    purpose: str,
    status: str,
    assistant_message_id: UUID | None = None,
    usage_owner_id: UUID | None = None,
    usage_owner_kind: str | None = None,
) -> None:
    """仅记录真实适配器已开始的调用；未知用量保留 NULL。"""
    metrics = getattr(provider, "last_call_metrics", None)
    if not isinstance(metrics, LLMCallMetrics):
        return
    async with manager.session() as session:
        session.add(ModelCall(
            conversation_id=conversation_id,
            assistant_message_id=assistant_message_id,
            purpose=purpose,
            status=status,
            model=metrics.model,
            duration_ms=metrics.duration_ms,
            prompt_tokens=metrics.prompt_tokens,
            completion_tokens=metrics.completion_tokens,
            total_tokens=metrics.total_tokens,
            finish_reason=metrics.finish_reason,
            usage_owner_id=usage_owner_id,
            usage_owner_kind=usage_owner_kind,
        ))
        await session.commit()
