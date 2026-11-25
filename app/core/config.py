from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "EchoNook API"
    # 环境值必须显式合法，拼写错误应在启动时失败，不能静默退回开发行为。
    app_env: Literal["development", "test", "production"] = "development"
    frontend_origin: str = "http://localhost:3000"
    # 数据库地址包含密码，SecretStr 可避免配置对象或异常日志意外泄露凭据。
    database_url: SecretStr = SecretStr("")
    test_database_url: SecretStr = SecretStr("")
    database_echo: bool = False
    llm_provider: Literal["demo", "openai_compatible"] = "demo"
    llm_api_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_reasoning_effort: Literal["low", "high", "max"] = "low"
    # 按当前接入模型填写；默认采用较小窗口，避免更换模型时沿用过大的预算。
    llm_context_window_tokens: int = Field(default=32768, gt=0)
    llm_max_output_tokens: int = Field(default=4096, gt=0)
    llm_token_safety_margin: int = Field(default=1024, ge=0)

    @model_validator(mode="after")
    def validate_token_budget(self) -> "Settings":
        """确保系统提示与当前问题至少有输入空间。"""
        if self.llm_max_output_tokens + self.llm_token_safety_margin >= self.llm_context_window_tokens:
            raise ValueError("LLM 输出预留和安全余量必须小于上下文窗口")
        return self

    def active_database_url(self) -> str:
        """根据运行环境选择数据库地址，仅在建立连接时取出真实字符串。"""
        value = self.test_database_url if self.app_env == "test" else self.database_url
        url = value.get_secret_value()
        if not url:
            variable = "TEST_DATABASE_URL" if self.app_env == "test" else "DATABASE_URL"
            raise ValueError(f"{variable} 未配置")
        return url


@lru_cache
def get_settings() -> Settings:
    """进程内缓存环境配置，保证所有组件读取同一份设置。"""
    return Settings()
