from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.persistence import stream_persisted_conversation
from app.conversations.models import Message, Principal
from app.conversations.repository import ConversationRepository
from app.conversations.summary import refresh_summary
from app.conversations.schemas import (
    ConversationCreate,
    ConversationList,
    ConversationRead,
    ConversationStreamRequest,
    MessagePage,
    StreamMessageRequest,
)
from app.conversations.service import (
    build_database_history, current_input_tokens, input_token_budget, stream_conversation,
)
from app.core.config import Settings
from app.core.dependencies import get_app_settings
from app.core.identity import require_identity
from app.db.dependencies import get_database_manager, get_db_session
from app.db.session import DatabaseManager
from app.llm.factory import create_llm_provider

router = APIRouter(prefix="/conversations", tags=["conversations"])


def conversation_not_found() -> HTTPException:
    """统一会话不存在时的状态码和机器可读错误码。"""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "conversation_not_found", "message": "会话不存在"},
    )


@router.post("", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_db_session),
    identity: Principal = Depends(require_identity),
) -> ConversationRead:
    """创建一个空会话，返回后可使用会话 ID 发送流式消息。"""
    repository = ConversationRepository(session)
    conversation = await repository.create(payload.title, owner_id=identity.id)
    # 写接口显式提交；提交前抛出的异常由 Session 上下文统一回滚。
    await session.commit()
    return ConversationRead.model_validate(conversation)


@router.get("", response_model=ConversationList)
async def list_conversations(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    identity: Principal = Depends(require_identity),
) -> ConversationList:
    """只列出当前游客或账号拥有的会话。"""
    items, total = await ConversationRepository(session).list(offset=offset, limit=limit, owner_id=identity.id)
    return ConversationList(items=items, total=total, offset=offset, limit=limit)


@router.get("/{conversation_id}", response_model=ConversationRead)
async def get_conversation(
    conversation_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    identity: Principal = Depends(require_identity),
) -> ConversationRead:
    """通过 URL 中的会话 ID 查询会话，不依赖列表前 100 条是否包含它。"""
    conversation = await ConversationRepository(session).get(conversation_id, owner_id=identity.id)
    if conversation is None:
        raise conversation_not_found()
    return ConversationRead.model_validate(conversation)


@router.get("/{conversation_id}/messages", response_model=MessagePage)
async def list_conversation_messages(
    conversation_id: UUID,
    before_sequence: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
    identity: Principal = Depends(require_identity),
) -> MessagePage:
    """按 sequence 游标读取会话消息，响应始终保持时间正序。"""
    repository = ConversationRepository(session)
    if not await repository.exists(conversation_id, owner_id=identity.id):
        raise conversation_not_found()
    items, has_more, next_before_sequence = await repository.list_messages(
        conversation_id,
        before_sequence=before_sequence,
        limit=limit,
        owner_id=identity.id,
    )
    return MessagePage(
        items=items,
        limit=limit,
        has_more=has_more,
        next_before_sequence=next_before_sequence,
    )


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: UUID,
    session: AsyncSession = Depends(get_db_session),
    identity: Principal = Depends(require_identity),
) -> Response:
    """删除会话及其全部消息；不存在时返回结构化 404。"""
    if not await ConversationRepository(session).delete(conversation_id, owner_id=identity.id):
        raise conversation_not_found()
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def create_streaming_response(events: AsyncIterator[str]) -> StreamingResponse:
    """统一 SSE 响应头，避免新旧流式接口出现代理缓冲差异。"""
    # StreamingResponse 会逐块转发生成器内容；X-Accel-Buffering 提示兼容的
    # 反向代理关闭缓冲，实际部署仍需代理配置配合。
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/{conversation_id}/messages:stream")
async def stream_conversation_message(
    conversation_id: UUID,
    request: ConversationStreamRequest,
    session: AsyncSession = Depends(get_db_session),
    manager: DatabaseManager = Depends(get_database_manager),
    settings: Settings = Depends(get_app_settings),
    identity: Principal = Depends(require_identity),
) -> StreamingResponse:
    """创建完整回合、加载数据库历史，并持久化助手流式状态。"""
    # 在写入回合之前校验当前问题；过长输入不能留下无法生成的占位消息。
    budget = input_token_budget(
        settings.llm_context_window_tokens,
        settings.llm_max_output_tokens,
        settings.llm_token_safety_margin,
    )
    if current_input_tokens(request.message) > budget:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": "input_budget_exceeded", "message": "当前问题超过模型输入预算，请缩短后重试"},
        )
    # 后续 commit/rollback 会使身份 ORM 对象过期；先提取调用归属快照。
    identity_id, identity_kind = identity.id, identity.kind
    repository = ConversationRepository(session)
    turn = await repository.start_turn(
        conversation_id,
        request.message,
        owner_id=identity_id,
    )
    if turn is None:
        raise conversation_not_found()
    user_message, assistant_message = turn

    # 必须先提交用户消息和助手占位消息再调用模型；提交会释放 start_turn
    # 获取的行锁，后续慢模型不会阻塞同会话的其他数据库操作。
    await session.commit()
    # SSE 在路由返回后才开始执行，因此先提取标量，不能让后台流访问稍后会
    # 过期或脱离 Session 的 ORM 对象。
    user_message_id = user_message.id
    assistant_message_id = assistant_message.id
    user_sequence = user_message.sequence
    assistant_sequence = assistant_message.sequence
    stored_history = await repository.load_model_history(
        conversation_id,
        before_sequence=user_sequence,
        scan_limit=None,
    )
    # rollback 会使 ORM 实例过期；先复制摘要和裁剪所需标量，避免模型阶段隐式访问数据库。
    history_snapshot = [
        Message(role=item.role, content=item.content, status=item.status, sequence=item.sequence)
        for item in stored_history
    ]
    # SELECT 会自动开启只读事务；模型流开始前显式结束它，避免慢模型长时间
    # 占用 PostgreSQL 连接。Session 本身稍后仍由依赖统一关闭。
    await session.rollback()
    provider = create_llm_provider(settings)
    # 摘要仅在真实模型下按需更新；失败时继续依靠近期原文回答。
    summary, covered_sequence = ("", -1)
    if settings.llm_provider == "openai_compatible":
        try:
            summary, covered_sequence = await refresh_summary(
                manager, provider, conversation_id, history_snapshot,
                usage_owner_id=identity_id, usage_owner_kind=identity_kind,
            )
        except Exception:
            # 摘要是增强功能；读取或更新失败时仍用近期原文完成本次问答。
            summary, covered_sequence = "", -1
    if current_input_tokens(request.message, summary) > budget:
        summary, covered_sequence = "", -1
    history = build_database_history(
        [item for item in history_snapshot if item.sequence > covered_sequence],
        token_budget=budget,
        current_message=request.message,
        context_summary=summary,
    )
    # 同一 Provider 先后服务摘要和主回复；主流尚未开始就断开时不能复用摘要指标。
    if hasattr(provider, "last_call_metrics"):
        provider.last_call_metrics = None
    stream_request = StreamMessageRequest(message=request.message, history=history, context_summary=summary)
    events = stream_persisted_conversation(
        stream_request,
        provider,
        manager,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        user_sequence=user_sequence,
        assistant_sequence=assistant_sequence,
        conversation_id=conversation_id,
        usage_owner_id=identity_id,
        usage_owner_kind=identity_kind,
    )
    return create_streaming_response(events)


@router.post("/messages:stream")
async def stream_message(
    request: StreamMessageRequest,
    settings: Settings = Depends(get_app_settings),
    identity: Principal = Depends(require_identity),
) -> StreamingResponse:
    """兼容旧前端：历史仍由客户端传入，S3 前保持该接口可用。"""
    provider = create_llm_provider(settings)
    return create_streaming_response(stream_conversation(request, provider))
