"""One gateway request: breaker check, rate limit, provider call with timeout, bounded retry.

Kept apart from the HTTP layer so the policy can be tested without FastAPI.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import structlog

from llm_gateway.providers.base import (
    NonRetryableProviderError,
    Provider,
    ProviderResult,
    RetryableProviderError,
)
from llm_gateway.resilience import (
    BreakerOpenError,
    BreakerState,
    CircuitBreaker,
    RateLimiter,
    backoff_delay,
)
from llm_gateway.settings import GatewaySettings

log = structlog.get_logger("llm_gateway")


@dataclass
class CallOutcome:
    # ok | retried_ok | failed | breaker_open | rate_limited  (llm_calls.outcome)
    outcome: str
    attempts: int
    retries: int
    latency_ms: float
    result: ProviderResult | None = None
    cost_usd: float = 0.0
    error_type: str | None = None
    # Why the call failed, returned to the client: breaker_open, retries_exhausted,
    # rate_limited, or a non-retryable error type (auth, bad_request, safety_block).
    reason: str | None = None
    breaker_state: BreakerState | None = None

    @property
    def unavailable(self) -> bool:
        """True when the client should get 503 (model unavailable), not a rejected request."""
        return self.reason in ("breaker_open", "retries_exhausted", "rate_limited")


def cost_usd(settings: GatewaySettings, prompt_tokens: int, completion_tokens: int) -> float:
    return round(
        prompt_tokens / 1000 * settings.llm_price_per_1k_prompt_tokens
        + completion_tokens / 1000 * settings.llm_price_per_1k_completion_tokens,
        8,
    )


class LLMService:
    def __init__(self, settings: GatewaySettings, provider: Provider, breaker: CircuitBreaker,
                 limiter: RateLimiter, *,
                 # Used for retry backoff only (injectable so tests can record delays).
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 on_retry: Callable[[], None] = lambda: None,
                 on_breaker_change: Callable[[BreakerState], None] = lambda s: None):
        self.settings = settings
        self.provider = provider
        self.breaker = breaker
        self.limiter = limiter
        self.sleep = sleep
        self.on_retry = on_retry
        self.on_breaker_change = on_breaker_change

    async def _wait_for_token(self, deadline: float) -> bool:
        wait_until = min(deadline, time.monotonic() + self.settings.llm_rate_limit_max_wait_seconds)
        while True:
            wait = await self.limiter.try_acquire()
            if wait <= 0:
                return True
            if time.monotonic() + wait > wait_until:
                return False
            log.info("rate_limit_wait", wait_s=round(wait, 2))
            await asyncio.sleep(wait)

    async def _provider_call(self, prompt: str, max_tokens: int | None, temperature: float,
                             timeout_s: float) -> ProviderResult:
        fault = self.settings.llm_fault_injection
        if fault != "off":
            log.warning("FAULT_INJECTED", fault_injection=fault)
            if fault == "timeout":
                await asyncio.sleep(timeout_s + 1)
            elif fault == "error_500":
                raise RetryableProviderError("injected HTTP 500", error_type="server_error",
                                             status_code=500)
            elif fault == "error_429":
                raise RetryableProviderError("injected HTTP 429",
                                             error_type="rate_limited_by_provider",
                                             status_code=429, retry_after_s=1.0)
        return await self.provider.generate(
            prompt, model=self.settings.llm_model or "", max_tokens=max_tokens,
            temperature=temperature, timeout_s=timeout_s,
        )

    async def complete(self, prompt: str, *, max_tokens: int | None,
                       temperature: float) -> CallOutcome:
        s = self.settings
        started = time.monotonic()
        deadline = started + s.llm_request_deadline_seconds
        attempts = retries = 0

        def done(outcome: str, **kwargs) -> CallOutcome:
            latency = round((time.monotonic() - started) * 1000, 1)
            return CallOutcome(outcome=outcome, attempts=attempts, retries=retries,
                               latency_ms=latency, **kwargs)

        while True:
            try:
                state = await self.breaker.before_call()
            except BreakerOpenError as exc:
                log.warning("breaker_fast_fail", breaker_state=exc.state, attempts=attempts)
                return done("breaker_open" if attempts == 0 else "failed",
                            error_type="breaker_open", reason="breaker_open",
                            breaker_state=exc.state)
            if state == BreakerState.half_open:
                log.info("breaker_trial_call")
                self.on_breaker_change(state)

            if not await self._wait_for_token(deadline):
                if state == BreakerState.half_open:
                    await self.breaker.release_trial()
                log.warning("rate_limited", attempts=attempts)
                return done("rate_limited" if attempts == 0 else "failed",
                            error_type="rate_limited", reason="rate_limited",
                            breaker_state=state)

            attempts += 1
            timeout_s = max(0.1, min(s.llm_timeout_seconds, deadline - time.monotonic()))
            error: RetryableProviderError
            try:
                result = await asyncio.wait_for(
                    self._provider_call(prompt, max_tokens, temperature, timeout_s), timeout_s)
            except TimeoutError:
                error = RetryableProviderError(f"no response within {timeout_s:.1f}s",
                                               error_type="timeout")
            except RetryableProviderError as exc:
                error = exc
            except NonRetryableProviderError as exc:
                new_state = await (self.breaker.record_failure() if exc.provider_unhealthy
                                   else self.breaker.record_success())
                self.on_breaker_change(new_state)
                log.warning("provider_rejected", error_type=exc.error_type, error=str(exc),
                            attempt=attempts)
                return done("failed", error_type=exc.error_type, reason=exc.error_type,
                            breaker_state=new_state)
            else:
                new_state = await self.breaker.record_success()
                self.on_breaker_change(new_state)
                return done("ok" if attempts == 1 else "retried_ok", result=result,
                            cost_usd=cost_usd(s, result.prompt_tokens, result.completion_tokens),
                            breaker_state=new_state)

            new_state = await self.breaker.record_failure()
            self.on_breaker_change(new_state)
            log.warning("provider_call_failed", error_type=error.error_type, error=str(error),
                        status_code=error.status_code, attempt=attempts,
                        breaker_state=new_state)
            if attempts > s.llm_max_retries:
                return done("failed", error_type=error.error_type, reason="retries_exhausted",
                            breaker_state=new_state)
            delay = backoff_delay(attempts, base=s.llm_backoff_base_seconds,
                                  maximum=s.llm_backoff_max_seconds,
                                  retry_after=error.retry_after_s)
            if time.monotonic() + delay >= deadline:
                log.warning("retry_skipped_deadline", delay_s=round(delay, 2))
                return done("failed", error_type=error.error_type, reason="retries_exhausted",
                            breaker_state=new_state)
            retries += 1
            self.on_retry()
            log.info("retry_scheduled", delay_s=round(delay, 2), retry=retries)
            await self.sleep(delay)
