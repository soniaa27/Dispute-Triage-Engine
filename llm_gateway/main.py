"""LLM gateway.  uvicorn --factory llm_gateway.main:create_app

LLM_MODE=stub      fixed response after STUB_DELAY_MS (dev, tests, Locust load tests).
LLM_MODE=provider  TODO(Person B): call the configured provider behind the same interface.
"""

import asyncio
import time

import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field

from common.logging import configure_logging
from llm_gateway.settings import GatewaySettings

log = structlog.get_logger("llm_gateway")

COMPLETIONS = Counter("llm_completions_total", "Completion requests", ["mode", "outcome"])
COMPLETION_LATENCY = Histogram("llm_completion_duration_seconds", "Completion latency", ["mode"])

STUB_TEXT = '{"stub": true, "note": "fixed response from llm-gateway stub mode"}'


class CompletionRequest(BaseModel):
    prompt: str = Field(min_length=1)
    system: str | None = None
    max_tokens: int = Field(default=512, ge=1, le=8192)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    metadata: dict[str, str] = Field(default_factory=dict)


class CompletionResponse(BaseModel):
    text: str
    mode: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: float


def _approx_tokens(text: str) -> int:
    # Rough 4-chars-per-token estimate; real provider mode reports actual usage.
    return max(1, len(text) // 4)


def create_app(settings: GatewaySettings | None = None) -> FastAPI:
    settings = settings or GatewaySettings()
    configure_logging("llm-gateway", settings.log_level)
    app = FastAPI(title="LLM Gateway", version="0.1.0")

    @app.middleware("http")
    async def bind_correlation(request: Request, call_next):
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=request.headers.get("X-Correlation-ID"),
            run_id=request.headers.get("X-Run-ID"),
        )
        return await call_next(request)

    @app.post("/v1/complete", response_model=CompletionResponse)
    async def complete(req: CompletionRequest):
        started = time.perf_counter()
        if settings.llm_mode != "stub":
            COMPLETIONS.labels(settings.llm_mode, "not_implemented").inc()
            raise HTTPException(status_code=501, detail="provider mode not implemented yet")

        await asyncio.sleep(settings.stub_delay_ms / 1000)
        latency = time.perf_counter() - started
        COMPLETIONS.labels("stub", "ok").inc()
        COMPLETION_LATENCY.labels("stub").observe(latency)
        prompt_text = (req.system or "") + req.prompt
        log.info("completion", mode="stub", latency_ms=round(latency * 1000, 1))
        return CompletionResponse(
            text=STUB_TEXT,
            mode="stub",
            model="stub",
            prompt_tokens=_approx_tokens(prompt_text),
            completion_tokens=_approx_tokens(STUB_TEXT),
            latency_ms=round(latency * 1000, 1),
        )

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "mode": settings.llm_mode}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app
