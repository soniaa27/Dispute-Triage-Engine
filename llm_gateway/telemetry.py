"""Gateway tracing (docs/observability.md). Called once from create_app.

The worker's httpx instrumentation sends the run's trace context with each /v1/complete
request, so the gateway's spans join the run's trace:

  POST /v1/complete (server span: run_id, correlation_id, step, prompt_version)
    llm.gateway    one gateway request: outcome, attempts, retries, tokens, cost, breaker
      llm.provider.<name>   one provider attempt: model, tokens, finish reason or error

The service and the provider are wrapped here, not edited: the resilience code (breaker,
rate limiter, retry) is unchanged.
"""

import structlog
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.trace import SpanKind, Status, StatusCode

from common.tracing import get_tracer, set_attributes, setup_tracing
from llm_gateway.providers.base import ProviderError, ProviderResult
from llm_gateway.service import CallOutcome, LLMService

EXCLUDED_URLS = "/healthz,/metrics,/v1/breaker"


def _request_attributes() -> dict[str, str]:
    """What the /v1/complete handler bound for logging: run_id, correlation_id, step and
    prompt_version (the spans' processor adds the first two to every span)."""
    bound = structlog.contextvars.get_contextvars()
    return {k: str(bound[k]) for k in ("run_id", "correlation_id", "step", "prompt_version")
            if bound.get(k)}


class TracedProvider:
    """One span per provider attempt."""

    def __init__(self, inner, model: str):
        self._inner = inner
        self.name = inner.name
        self._model = model

    async def generate(self, prompt, *, model, max_tokens, temperature,
                       timeout_s) -> ProviderResult:
        attributes = {**_request_attributes(), "provider": self.name, "model": self._model,
                      "timeout_s": timeout_s}
        with get_tracer().start_as_current_span(f"llm.provider.{self.name}", kind=SpanKind.CLIENT,
                                                attributes=attributes) as span:
            try:
                result = await self._inner.generate(prompt, model=model, max_tokens=max_tokens,
                                                    temperature=temperature,
                                                    timeout_s=timeout_s)
            except ProviderError as exc:
                set_attributes(span, error_type=exc.error_type, status_code=exc.status_code)
                span.set_status(Status(StatusCode.ERROR, f"{exc.error_type}: {exc}"))
                raise
            set_attributes(span, model=result.model, prompt_tokens=result.prompt_tokens,
                           completion_tokens=result.completion_tokens,
                           finish_reason=result.raw_finish_reason)
            return result


def _traced_complete(service: LLMService, provider: str, model: str):
    complete = service.complete

    async def traced(prompt: str, *, max_tokens: int | None, temperature: float) -> CallOutcome:
        attributes = {**_request_attributes(), "provider": provider, "model": model}
        set_attributes(trace.get_current_span(), **attributes)
        with get_tracer().start_as_current_span("llm.gateway", attributes=attributes) as span:
            outcome = await complete(prompt, max_tokens=max_tokens, temperature=temperature)
            result = outcome.result
            set_attributes(span, outcome=outcome.outcome, attempts=outcome.attempts,
                           retries=outcome.retries, latency_ms=outcome.latency_ms,
                           cost_usd=outcome.cost_usd, error_type=outcome.error_type,
                           reason=outcome.reason, breaker_state=outcome.breaker_state,
                           prompt_tokens=result.prompt_tokens if result else None,
                           completion_tokens=result.completion_tokens if result else None)
            if result is None:
                span.set_status(Status(StatusCode.ERROR, outcome.reason or outcome.outcome))
            return outcome

    return traced


def instrument_gateway(app) -> None:
    setup_tracing("llm-gateway")
    service: LLMService = app.state.service
    provider, model = service.settings.llm_provider, service.settings.model_name
    service.provider = TracedProvider(service.provider, model)
    service.complete = _traced_complete(service, provider, model)
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS,
                                       exclude_spans=["receive", "send"])
