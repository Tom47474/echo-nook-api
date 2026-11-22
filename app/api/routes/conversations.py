from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from app.conversations.schemas import StreamMessageRequest
from app.conversations.service import stream_conversation
from app.core.config import Settings, get_settings
from app.llm.factory import create_llm_provider

router = APIRouter(prefix="/conversations", tags=["conversations"])


@router.post("/messages:stream")
async def stream_message(
    request: StreamMessageRequest, settings: Settings = Depends(get_settings)
) -> StreamingResponse:
    provider = create_llm_provider(settings)
    return StreamingResponse(
        stream_conversation(request, provider),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
