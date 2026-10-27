"""LLM gateway.  uvicorn --factory llm_gateway.main:create_app

The single place that talks to a language model provider. LLM_PROVIDER picks it:
  stub    fixed response after STUB_DELAY_MS (dev, tests, Locust load tests)
  gemini  Google Gemini via google-genai
Every call goes through the breaker, the rate limiter, a timeout and bounded retry, and is
recorded in llm_calls with tokens and cost. See docs/llm_gateway.md.
"""

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.engine import Engine

from common.db import LlmCall, make_engine, make_session_factory
from common.logging import configure_logging
from contracts.llm import LLMResponse
from llm_gateway.providers import build_provider
from llm_gateway.providers.base import Provider
from llm_gateway.resilience import STATE_GAUGE, BreakerState, CircuitBreaker, RateLimiter
from llm_gateway.service import CallOutcome, LLMService
from llm_gateway.settings import GatewaySettings

log = structlog.get_logger("llm_gateway")

REQUESTS = Counter("llm_requests_total", "Gateway completion requests",
                   ["provider", "model", "step", "outcome"])
TOKENS = Counter("llm_tokens_total", "Tokens used, from provider usage metadata",
                 ["type", "step"])
COST = Counter("llm_cost_usd_total", "Cost in USD, computed at call time", ["step"])
LATENCY = Histogram("llm_latency_seconds", "Gateway request latency, including retries",
                    ["step"], buckets=(0.25, 0.5, 1, 2, 4, 8, 15, 30, 60, 120))
RETRIES = Counter("llm_retries_total", "Provider call retries")
CIRCUIT = Gauge("llm_circuit_state", "Circuit breaker state: 0 closed, 1 half-open, 2 open")


class CompletionRequest(BaseModel):
    prompt: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1, max_length=64)
    step: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)


def _unavailable_body(outcome: CallOutcome) -> dict:
    return {"error": "llm_unavailable", "reason": outcome.reason,
            "breaker_state": outcome.breaker_state}


def create_app(settings: GatewaySettings | None = None, *, redis: Redis | None = None,
               engine: Engine | None = None, provider: Provider | None = None,
               sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> FastAPI:
    settings = settings or GatewaySettings()
    secret = settings.llm_api_key.get_secret_value() if settings.llm_api_key else ""
    configure_logging("llm-gateway", settings.log_level, redact=(secret,))
    # httpx logs every provider request at INFO; keep the gateway's own lines as the record.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    redis = redis or Redis.from_url(settings.redis_url)
    engine = engine or make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    breaker = CircuitBreaker(
        redis, failure_threshold=settings.llm_cb_failure_threshold,
        reset_seconds=settings.llm_cb_reset_seconds,
        trial_ttl_seconds=settings.llm_request_deadline_seconds + 5,
    )
    limiter = RateLimiter(redis, requests_per_minute=settings.llm_max_requests_per_minute)
    service = LLMService(
        settings, provider or build_provider(settings), breaker, limiter, sleep=sleep,
        on_retry=RETRIES.inc, on_breaker_change=lambda s: CIRCUIT.set(STATE_GAUGE[s]),
    )

    app = FastAPI(title="LLM Gateway", version="0.2.0")
    app.state.service = service
    log.info("gateway_started", provider=settings.llm_provider, model=settings.model_name,
             max_requests_per_minute=settings.llm_max_requests_per_minute,
             fault_injection=settings.llm_fault_injection)
    if settings.llm_fault_injection != "off":
        log.warning("FAULT INJECTION ENABLED: every provider call will fail",
                    fault_injection=settings.llm_fault_injection)

    def persist(row: LlmCall) -> None:
        try:
            with sessions() as session, session.begin():
                session.add(row)
        except Exception as exc:  # recording must never fail the call itself
            log.error("llm_call_persist_failed", error=f"{exc.__class__.__name__}: {exc}")

    @app.post("/v1/complete", response_model=LLMResponse)
    async def complete(req: CompletionRequest, request: Request):
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id, run_id=req.run_id, step=req.step,
            prompt_version=req.prompt_version,
        )
        log.info("llm_request", prompt_chars=len(req.prompt),
                 prompt_sha256=hashlib.sha256(req.prompt.encode()).hexdigest()[:16],
                 max_tokens=req.max_tokens, temperature=req.temperature)
        log.debug("llm_prompt", prompt=req.prompt)

        outcome = await service.complete(req.prompt, max_tokens=req.max_tokens,
                                         temperature=req.temperature)
        result = outcome.result
        model = result.model if result else settings.model_name
        await asyncio.to_thread(persist, LlmCall(
            run_id=req.run_id, correlation_id=correlation_id, step=req.step,
            prompt_version=req.prompt_version, provider=settings.llm_provider, model=model,
            prompt_tokens=result.prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            cost_usd=outcome.cost_usd, latency_ms=outcome.latency_ms,
            attempts=outcome.attempts, outcome=outcome.outcome, error_type=outcome.error_type,
        ))
        REQUESTS.labels(settings.llm_provider, model, req.step, outcome.outcome).inc()
        LATENCY.labels(req.step).observe(outcome.latency_ms / 1000)
        headers = {"X-Correlation-ID": correlation_id}

        if result is None:
            log.warning("llm_response", outcome=outcome.outcome, reason=outcome.reason,
                        error_type=outcome.error_type, attempts=outcome.attempts,
                        latency_ms=outcome.latency_ms, breaker_state=outcome.breaker_state)
            if outcome.unavailable:
                return JSONResponse(_unavailable_body(outcome), status_code=503, headers=headers)
            return JSONResponse({"error": "llm_request_rejected", "reason": outcome.reason,
                                 "breaker_state": outcome.breaker_state},
                                status_code=502, headers=headers)

        TOKENS.labels("prompt", req.step).inc(result.prompt_tokens)
        TOKENS.labels("completion", req.step).inc(result.completion_tokens)
        COST.labels(req.step).inc(outcome.cost_usd)
        log.info("llm_response", outcome=outcome.outcome, model=model,
                 prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                 cost_usd=outcome.cost_usd, latency_ms=outcome.latency_ms,
                 attempts=outcome.attempts, finish_reason=result.raw_finish_reason)
        response = LLMResponse(
            text=result.text, prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens, cost_usd=outcome.cost_usd, model=model,
            latency_ms=outcome.latency_ms, prompt_version=req.prompt_version, step=req.step,
        )
        return JSONResponse(response.model_dump(), headers=headers)

    @app.get("/v1/breaker")
    async def breaker_status():
        snapshot = await breaker.snapshot()
        CIRCUIT.set(STATE_GAUGE[BreakerState(snapshot["state"])])
        return {**snapshot, "provider": settings.llm_provider,
                "fault_injection": settings.llm_fault_injection,
                "max_requests_per_minute": settings.llm_max_requests_per_minute}

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "provider": settings.llm_provider,
                "fault_injection": settings.llm_fault_injection}

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        try:
            CIRCUIT.set(STATE_GAUGE[await breaker.state()])
        except Exception as exc:  # metrics must still render if Redis is down
            log.warning("breaker_state_unavailable", error=str(exc))
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
