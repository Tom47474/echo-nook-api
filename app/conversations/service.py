import asyncio
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
MESSAGE_OVERHEAD_TOKENS = 16


def estimate_message_tokens(content: str) -> int:
    """用 UTF-8 字节数加消息包装余量做保守估算；不是供应商实际 token 用量。"""
    return len(content.encode("utf-8")) + MESSAGE_OVERHEAD_TOKENS


def input_token_budget(context_window: int, max_output: int, safety_margin: int) -> int:
    """预留模型输出及估算误差，返回本次完整输入允许使用的预算。"""
    return context_window - max_output - safety_margin


def current_input_tokens(message: str, summary: str = "") -> int:
    """系统提示和当前问题必须先占用预算，历史只能使用剩余部分。"""
    summary_tokens = estimate_message_tokens(summary) if summary else 0
    return estimate_message_tokens(SYSTEM_PROMPT) + estimate_message_tokens(message) + summary_tokens


def build_database_history(
    messages: Sequence[Message],
    *,
    max_messages: int = 20,
    token_budget: int | None = None,
    current_message: str = "",
    context_summary: str = "",
) -> list[ChatMessage]:
    """按完整回合选择最近历史，同时限制消息条数和估算输入预算。"""
    remaining = token_budget - current_input_tokens(current_message, context_summary) if token_budget is not None else None
    if remaining is not None and remaining < 0:
        raise ValueError("当前问题超过模型输入预算")
    turns: list[list[Message]] = []
    current_turn: list[Message] = []
    for message in messages:
        if message.role == MessageRole.USER.value:
            if current_turn:
                turns.append(current_turn)
            # 空问题和未完成问题不是可复用的历史回合。
            current_turn = [message] if message.status == MessageStatus.COMPLETED.value and message.content.strip() else []
        elif current_turn and message.role == MessageRole.ASSISTANT.value and message.status in {
            MessageStatus.COMPLETED.value,
            MessageStatus.FAILED.value,
            MessageStatus.CANCELLED.value,
        }:
            # 若同一用户消息之后存在多次助手尝试，以最后一个终态为准。
            # 最后一次为空时保留用户问题，但不能退回旧回答冒充新结果。
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
        turn_tokens = sum(estimate_message_tokens(item.content) for item in context_turn)
        if remaining is not None and turn_tokens > remaining:
            # 旧回合不能拆开纳入；更旧内容也不越过这个回合重新挑选。
            break
        selected.insert(0, context_turn)
        count += len(context_turn)
        if remaining is not None:
            remaining -= turn_tokens
    return [message for turn in selected for message in turn]


async def stream_conversation(
    request: StreamMessageRequest,
    provider: LLMProvider,
    *,
    timeout_seconds: float | None = None,
) -> AsyncIterator[str]:
    """调用模型并把分片、完成或失败结果转换为统一 SSE 事件。"""
    # 模型收到的顺序固定为历史消息在前、本次用户问题在后。
    messages = [*request.history, ChatMessage(role="user", content=request.message)]
    # 摘要是系统侧背景信息，不作为用户或助手的历史发言展示。
    system_prompt = SYSTEM_PROMPT + ("\n\n较早对话摘要（仅作背景）：\n" + request.context_summary if request.context_summary else "")
    provider_stream = provider.stream_reply(messages, system_prompt)
    try:
        # 总超时覆盖首字等待和后续全部分片；到期后关闭 Provider 流并返回可重试错误。
        async with asyncio.timeout(timeout_seconds):
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
    except TimeoutError:
        yield encode_event(
            "message.failed",
            {"code": "provider_timeout", "message": "模型回复超过总时限，请稍后重试"},
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
