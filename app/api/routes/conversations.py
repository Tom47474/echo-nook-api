from collections.abc import AsyncIterator
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

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
from app.db.dependencies import get_db_session
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
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """保存当前问题、从数据库加载历史，再启动指定会话的模型流。"""
    repository = ConversationRepository(session)
    user_message = await repository.append_user_message(
        conversation_id,
        request.message,
    )
    if user_message is None:
        raise conversation_not_found()

    # 必须先提交用户消息再调用模型：即使模型配置错误或网络失败，用户问题
    # 仍能在数据库中恢复。提交也会释放 append_user_message 获取的行锁。
    await session.commit()
    stored_history = await repository.load_model_history(
        conversation_id,
        before_sequence=user_message.sequence,
    )
    history = build_database_history(stored_history)
    # SELECT 会自动开启只读事务；模型流开始前显式结束它，避免慢模型长时间
    # 占用 PostgreSQL 连接。Session 本身稍后仍由依赖统一关闭。
    await session.rollback()
    provider = create_llm_provider(settings)
    stream_request = StreamMessageRequest(message=request.message, history=history)
    return create_streaming_response(stream_conversation(stream_request, provider))


@router.post("/messages:stream")
async def stream_message(
    request: StreamMessageRequest,
    settings: Settings = Depends(get_app_settings),
) -> StreamingResponse:
    """兼容旧前端：历史仍由客户端传入，S3 前保持该接口可用。"""
    provider = create_llm_provider(settings)
    return create_streaming_response(stream_conversation(request, provider))
