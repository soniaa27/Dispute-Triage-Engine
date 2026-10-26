from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    llm_mode: Literal["stub", "provider"] = "stub"
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    stub_delay_ms: int = 1500
    # USD per 1,000 tokens, used to compute LLMResponse.cost_usd at call time.
    llm_price_per_1k_prompt_tokens: float = Field(default=0, ge=0)
    llm_price_per_1k_completion_tokens: float = Field(default=0, ge=0)
    log_level: str = "INFO"
