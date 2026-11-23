import json
from collections.abc import AsyncIterator, Sequence

import httpx

from app.conversations.models import Message, MessageRole, MessageStatus
from app.conversations.schemas import ChatMessage, StreamMessageRequest
from app.llm.base import LLMProvider


SYSTEM_PROMPT = (
    "你是 EchoNook 的虚拟频道主持人。用用户使用的语言回答，表达自然、简洁。"
    "不确定的事实要说明，不要假装拥有实时信息。"
)


def encode_event(name: str, data: dict[str, str]) -> str:
    """把业务事件编码成浏览器 EventSource/fetch 可解析的 SSE 帧。"""
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


INTERRUPTED_SUFFIX = "\n\n[系统注记：这条回复未完整生成。]"


def build_database_history(
    messages: Sequence[Message],
    *,
    max_messages: int = 20,
) -> list[ChatMessage]:
    """把数据库消息整理为完整回合，并限制为模型可接收的最近 20 条。"""
    turns: list[list[Message]] = []
    current_turn: list[Message] = []
    for message in messages:
        if message.role == MessageRole.USER.value:
            if current_turn:
                turns.append(current_turn)
            current_turn = [message]
        elif current_turn and message.role == MessageRole.ASSISTANT.value:
            # 同一回合可能因重新生成存在多条助手消息，只保留最后一条。
            current_turn = [current_turn[0], message]
    if current_turn:
        turns.append(current_turn)

    selected: list[list[ChatMessage]] = []
    count = 0
    for turn in reversed(turns):
        user = turn[0]
        context_turn = [ChatMessage(role="user", content=user.content[:4000])]
        if len(turn) > 1 and turn[1].content.strip():
            assistant = turn[1]
            content = assistant.content[:4000]
            if assistant.status in {
                MessageStatus.FAILED.value,
                MessageStatus.CANCELLED.value,
            }:
                # 中断标记也计入单条 4000 字符限制，避免内部数据库数据绕过
                # 旧接口对模型上下文的长度约束。
                content = content[: 4000 - len(INTERRUPTED_SUFFIX)] + INTERRUPTED_SUFFIX
            context_turn.append(ChatMessage(role="assistant", content=content))
        if count + len(context_turn) > max_messages:
            break
        selected.insert(0, context_turn)
        count += len(context_turn)
    return [message for turn in selected for message in turn]


async def stream_conversation(
    request: StreamMessageRequest, provider: LLMProvider
) -> AsyncIterator[str]:
    """调用模型并把分片、完成或失败结果转换为统一 SSE 事件。"""
    # 模型收到的顺序固定为历史消息在前、本次用户问题在后。
    messages = [*request.history, ChatMessage(role="user", content=request.message)]
    provider_stream = provider.stream_reply(messages, SYSTEM_PROMPT)
    try:
        async for chunk in provider_stream:
            if chunk:
                yield encode_event("message.delta", {"text": chunk})
    except ValueError:
        yield encode_event(
            "message.failed",
            {
                "code": "provider_not_configured",
                "message": "请先配置服务端模型 API Key 和模型 ID",
            },
        )
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
        yield encode_event(
            "message.failed",
            {"code": "provider_timeout", "message": "模型服务响应超时，请稍后重试"},
        )
        return
    except httpx.RequestError:
        yield encode_event(
            "message.failed",
            {"code": "provider_unreachable", "message": "无法连接模型服务，请稍后重试"},
        )
        return
    except Exception:
        yield encode_event(
            "message.failed",
            {"code": "generation_failed", "message": "回复生成失败，请稍后重试"},
        )
        return
    else:
        yield encode_event("message.completed", {})
    finally:
        # 浏览器断开只会关闭外层生成器，因此必须继续关闭 Provider 流，
        # 才能让 httpx 及时释放上游连接。
        close = getattr(provider_stream, "aclose", None)
        if close is not None:
            await close()
