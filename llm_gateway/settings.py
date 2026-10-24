from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    llm_mode: Literal["stub", "provider"] = "stub"
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    stub_delay_ms: int = 1500
    log_level: str = "INFO"
