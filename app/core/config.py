from functools import lru_cache
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "EchoNook API"
    # Keep deployment behavior explicit; typos must fail during startup rather
    # than silently selecting development behavior in a production process.
    app_env: Literal["development", "test", "production"] = "development"
    frontend_origin: str = "http://localhost:3000"
    # Connection URLs contain credentials, so SecretStr prevents accidental
    # disclosure if settings are logged or included in an exception.
    database_url: SecretStr = SecretStr("")
    test_database_url: SecretStr = SecretStr("")
    database_echo: bool = False
    llm_provider: Literal["demo", "openai_compatible"] = "demo"
    llm_api_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_reasoning_effort: Literal["low", "high", "max"] = "low"

    def active_database_url(self) -> str:
        """Return the runtime URL without exposing it through normal repr output."""
        value = self.test_database_url if self.app_env == "test" else self.database_url
        url = value.get_secret_value()
        if not url:
            variable = "TEST_DATABASE_URL" if self.app_env == "test" else "DATABASE_URL"
            raise ValueError(f"{variable} 未配置")
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
