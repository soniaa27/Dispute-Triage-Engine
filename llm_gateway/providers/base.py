"""What every provider implements, and the errors the gateway's retry and breaker act on."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ProviderResult:
    text: str
    # As reported by the provider's usage metadata, never estimated (stub excepted).
    prompt_tokens: int
    completion_tokens: int
    model: str
    raw_finish_reason: str | None


class ProviderError(Exception):
    """Base class. `error_type` is a short label stored in llm_calls.error_type."""

    def __init__(self, message: str, *, error_type: str, status_code: int | None = None):
        super().__init__(message)
        self.error_type = error_type
        self.status_code = status_code


class RetryableProviderError(ProviderError):
    """Timeouts, HTTP 429, 5xx, connection errors. Retried with backoff; counts as a breaker
    failure."""

    def __init__(self, message: str, *, error_type: str, status_code: int | None = None,
                 retry_after_s: float | None = None):
        super().__init__(message, error_type=error_type, status_code=status_code)
        self.retry_after_s = retry_after_s


class NonRetryableProviderError(ProviderError):
    """Auth, bad request, safety block. Never retried.

    `provider_unhealthy` is True when the provider itself is unusable (auth failure), so the
    breaker counts it; a bad request or a safety block says nothing about provider health.
    """

    def __init__(self, message: str, *, error_type: str, status_code: int | None = None,
                 provider_unhealthy: bool = False):
        super().__init__(message, error_type=error_type, status_code=status_code)
        self.provider_unhealthy = provider_unhealthy


class Provider(Protocol):
    name: str

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int | None,
        temperature: float,
        timeout_s: float,
    ) -> ProviderResult:
        """One completion. Raises RetryableProviderError or NonRetryableProviderError."""
        ...
