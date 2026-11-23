import json
from collections.abc import AsyncIterator

import httpx

from app.conversations.schemas import ChatMessage, StreamMessageRequest
from app.llm.base import LLMProvider


SYSTEM_PROMPT = (
    "你是 EchoNook 的虚拟频道主持人。用用户使用的语言回答，表达自然、简洁。"
    "不确定的事实要说明，不要假装拥有实时信息。"
)


def encode_event(name: str, data: dict[str, str]) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def stream_conversation(
    request: StreamMessageRequest, provider: LLMProvider
) -> AsyncIterator[str]:
    messages = [*request.history, ChatMessage(role="user", content=request.message)]
    provider_stream = provider.stream_reply(messages, SYSTEM_PROMPT)
    try:
        async for chunk in provider_stream:
            if chunk:
                yield encode_event("message.delta", {"text": chunk})
    except ValueError:
        yield encode_event("message.failed", {"code": "provider_not_configured", "message": "请先配置服务端模型 API Key 和模型 ID"})
        return
    except httpx.HTTPStatusError as error:
        status = error.response.status_code
        if status == 401 or status == 403:
            code, message = "provider_auth_failed", "模型服务鉴权失败，请检查 API Key 和权限"
        elif status == 404:
            code, message = "model_not_available", "模型或接入点不可用，请检查模型 ID 和开通状态"
        elif status == 429:
            code, message = "provider_rate_limited", "模型服务请求过多，请稍后重试"
        else:
            code, message = "generation_failed", "模型服务暂时不可用，请稍后重试"
        yield encode_event("message.failed", {"code": code, "message": message})
        return
    except httpx.TimeoutException:
        yield encode_event("message.failed", {"code": "provider_timeout", "message": "模型服务响应超时，请稍后重试"})
        return
    except httpx.RequestError:
        yield encode_event("message.failed", {"code": "provider_unreachable", "message": "无法连接模型服务，请稍后重试"})
        return
    except Exception:
        yield encode_event("message.failed", {"code": "generation_failed", "message": "回复生成失败，请稍后重试"})
        return
    else:
        yield encode_event("message.completed", {})
    finally:
        # Closing the browser response does not automatically close a nested
        # async generator. Propagate the close so httpx can release the model
        # provider's streaming response instead of continuing in the background.
        close = getattr(provider_stream, "aclose", None)
        if close is not None:
            await close()
