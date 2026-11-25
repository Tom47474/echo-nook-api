"""模型适配器提供的单次真实调用信息；缺失字段保持 None。"""

from dataclasses import dataclass


@dataclass
class LLMCallMetrics:
    model: str | None = None
    duration_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None
