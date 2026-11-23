from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.persistence import stream_persisted_conversation
from app.conversations.repository import ConversationRepository
from app.conversations.schemas import (
    ConversationCreate,
    ConversationList,
    ConversationRead,
    ConversationStreamRequest,
    MessagePage,
    StreamMessageRequest,
)
from app.conversations.service import build_database_history, stream_conversation
from app.core.config import Settings
from app.core.dependencies import get_app_settings
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
) -> ConversationRead:
    """创建一个空会话，返回后可使用会话 ID 发送流式消息。"""
    repository = ConversationRepository(session)
    conversation = await repository.create(payload.title)
    # 写接口显式提交；提交前抛出的异常由 Session 上下文统一回滚。
    await session.commit()
    return ConversationRead.model_validate(conversation)


@router.get("", response_model=ConversationList)
async def list_conversations(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationList:
    """分页列出全部会话，当前阶段尚未按用户身份过滤。"""
    items, total = await ConversationRepository(session).list(offset=offset, limit=limit)
    return ConversationList(items=items, total=total, offset=offset, limit=limit)


@router.get("/{conversation_id}/messages", response_model=MessagePage)
async def list_conversation_messages(
    conversation_id: UUID,
    before_sequence: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> MessagePage:
    """按 sequence 游标读取会话消息，响应始终保持时间正序。"""
    repository = ConversationRepository(session)
    if not await repository.exists(conversation_id):
        raise conversation_not_found()
    items, has_more, next_before_sequence = await repository.list_messages(
        conversation_id,
        before_sequence=before_sequence,
        limit=limit,
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
) -> Response:
    """删除会话及其全部消息；不存在时返回结构化 404。"""
    if not await ConversationRepository(session).delete(conversation_id):
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
) -> StreamingResponse:
    """创建完整回合、加载数据库历史，并持久化助手流式状态。"""
    repository = ConversationRepository(session)
    turn = await repository.start_turn(
        conversation_id,
        request.message,
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
    )
    history = build_database_history(stored_history)
    # SELECT 会自动开启只读事务；模型流开始前显式结束它，避免慢模型长时间
    # 占用 PostgreSQL 连接。Session 本身稍后仍由依赖统一关闭。
    await session.rollback()
    provider = create_llm_provider(settings)
    stream_request = StreamMessageRequest(message=request.message, history=history)
    events = stream_persisted_conversation(
        stream_request,
        provider,
        manager,
        user_message_id=user_message_id,
        assistant_message_id=assistant_message_id,
        user_sequence=user_sequence,
        assistant_sequence=assistant_sequence,
    )
    return create_streaming_response(events)


@router.post("/messages:stream")
async def stream_message(
    request: StreamMessageRequest,
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """兼容旧前端：历史仍由客户端传入，S3 前保持该接口可用。"""
    provider = create_llm_provider(settings)
    return create_streaming_response(stream_conversation(request, provider))
