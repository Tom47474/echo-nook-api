from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "EchoNook API"
    app_env: str = "development"
    frontend_origin: str = "http://localhost:3000"
    llm_provider: Literal["demo", "openai_compatible"] = "demo"
    llm_api_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_reasoning_effort: Literal["low", "high", "max"] = "low"


@lru_cache
def get_settings() -> Settings:
    return Settings()
