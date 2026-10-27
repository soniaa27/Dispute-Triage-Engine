from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class GatewaySettings(BaseSettings):
    """Loaded from environment variables (see .env.example). No endpoint or model defaults."""

    model_config = SettingsConfigDict(extra="ignore")

    # Provider selection: a key of llm_gateway.providers.PROVIDERS.
    llm_provider: Literal["stub", "gemini"] = "stub"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None
    stub_delay_ms: int = Field(default=1500, ge=0)
    # USD per 1,000 tokens, used to compute LLMResponse.cost_usd at call time.
    llm_price_per_1k_prompt_tokens: float = Field(default=0, ge=0)
    llm_price_per_1k_completion_tokens: float = Field(default=0, ge=0)

    # Resilience (requirement A5).
    llm_timeout_seconds: float = Field(default=30, gt=0)
    # Whole request budget: rate-limit wait + every attempt + backoff. HttpLLMClient waits a
    # little longer than this.
    llm_request_deadline_seconds: float = Field(default=60, gt=0)
    llm_max_retries: int = Field(default=3, ge=0)
    llm_backoff_base_seconds: float = Field(default=1, ge=0)
    llm_backoff_max_seconds: float = Field(default=10, ge=0)
    llm_cb_failure_threshold: int = Field(default=5, ge=1)
    llm_cb_reset_seconds: float = Field(default=30, gt=0)
    llm_max_requests_per_minute: float = Field(default=4, gt=0)
    llm_rate_limit_max_wait_seconds: float = Field(default=20, ge=0)
    llm_fault_injection: Literal["off", "timeout", "error_500", "error_429"] = "off"

    redis_url: str
    database_url: str
    log_level: str = "INFO"

    @field_validator("llm_provider", "llm_fault_injection", mode="before")
    @classmethod
    def _normalise(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("llm_model", mode="before")
    @classmethod
    def _blank_is_none(cls, value):
        return (value.strip() or None) if isinstance(value, str) else value

    @model_validator(mode="after")
    def _provider_configured(self) -> "GatewaySettings":
        if self.llm_provider != "stub":
            if not self.llm_model:
                raise ValueError(f"LLM_MODEL is required when LLM_PROVIDER={self.llm_provider}")
            if self.llm_api_key is None or not self.llm_api_key.get_secret_value().strip():
                raise ValueError(f"LLM_API_KEY is required when LLM_PROVIDER={self.llm_provider}")
        return self

    @property
    def model_name(self) -> str:
        """Model recorded for the call (the stub ignores LLM_MODEL)."""
        return "stub" if self.llm_provider == "stub" else str(self.llm_model)
