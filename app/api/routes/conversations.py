from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.repository import ConversationRepository
from app.conversations.schemas import (
    ConversationCreate,
    ConversationList,
    ConversationRead,
    MessagePage,
    StreamMessageRequest,
)
from app.conversations.service import stream_conversation
from app.core.config import Settings, get_settings
from app.db.dependencies import get_db_session
from app.llm.factory import create_llm_provider

router = APIRouter(prefix="/conversations", tags=["conversations"])


def conversation_not_found() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "conversation_not_found", "message": "会话不存在"},
    )


@router.post("", response_model=ConversationRead, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    payload: ConversationCreate,
    session: AsyncSession = Depends(get_db_session),
) -> ConversationRead:
    repository = ConversationRepository(session)
    conversation = await repository.create(payload.title)
    # Mutating endpoints commit explicitly; the session dependency rolls back if
    # an exception escapes before this point.
    await session.commit()
    return ConversationRead.model_validate(conversation)


@router.get("", response_model=ConversationList)
async def list_conversations(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationList:
    items, total = await ConversationRepository(session).list(offset=offset, limit=limit)
    return ConversationList(items=items, total=total, offset=offset, limit=limit)


@router.get("/{conversation_id}/messages", response_model=MessagePage)
async def list_conversation_messages(
    conversation_id: UUID,
    before_sequence: int | None = Query(default=None, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
) -> MessagePage:
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
    if not await ConversationRepository(session).delete(conversation_id):
        raise conversation_not_found()
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/messages:stream")
async def stream_message(
    request: StreamMessageRequest, settings: Settings = Depends(get_settings)
) -> StreamingResponse:
    provider = create_llm_provider(settings)
    # StreamingResponse forwards each generator chunk immediately. Compatible
    # reverse proxies also use X-Accel-Buffering to avoid buffering the SSE body.
    return StreamingResponse(
        stream_conversation(request, provider),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
