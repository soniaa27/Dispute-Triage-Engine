"""FastAPI app: dispute ingestion, run status, quarantine, feedback, quality, health, metrics.

Run with:  uvicorn --factory api.main:create_app
"""

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

import structlog
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError
from redis import Redis
from rq import Queue
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from api.controls import (
    DisputeCache,
    RateLimiter,
    client_id,
    dispute_key,
    reusable,
)
from api.metrics import (
    CACHE_REQUESTS,
    DISPUTES_ACCEPTED,
    DISPUTES_QUARANTINED,
    GUARDRAIL_INPUT_FLAGS,
)
from api.middleware import CorrelationIdMiddleware
from api.settings import ApiSettings
from api.telemetry import setup_observability
from common.db import Feedback, Quarantine, Run, init_db, make_engine, make_session_factory
from common.logging import configure_logging
from common.quality import summarize
from common.tracing import job_meta
from contracts import (
    DisputeAccepted,
    DisputeRequest,
    FeedbackAccepted,
    FeedbackRequest,
    RunResult,
    RunStatus,
    RunView,
)
from guardrails import check_toxicity, detect_injection

# Job function path; the API never imports worker/agent code.
JOB_FUNC = "worker.tasks.process_run"
# Hard backstop for RQ beyond the worker's own wall-clock timeout.
RQ_TIMEOUT_GRACE_SECONDS = 30

log = structlog.get_logger("api")


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    # Round-trip through JSON so every error (including ctx values) is serialisable.
    return json.loads(exc.json(include_url=False))


def _count_guardrail_flags(dispute: DisputeRequest) -> None:
    """Count what the input guardrails will find; the worker enforces them."""
    injection = detect_injection(dispute.complaint_text)
    if injection.flagged:
        GUARDRAIL_INPUT_FLAGS.labels("injection").inc()
    for category in check_toxicity(injection.sanitized_text).categories:
        GUARDRAIL_INPUT_FLAGS.labels(category).inc()
    if injection.flagged:
        log.warning("guardrail_input_flagged", injection_rules=injection.rules)


def create_app(
    settings: ApiSettings | None = None,
    redis: Redis | None = None,
    engine: Engine | None = None,
) -> FastAPI:
    settings = settings or ApiSettings()
    configure_logging("api", settings.log_level)
    engine = engine or make_engine(settings.database_url)
    redis = redis or Redis.from_url(settings.redis_url)
    session_factory = make_session_factory(engine)
    queue = Queue(settings.queue_name, connection=redis)
    limiter = RateLimiter(redis, settings.rate_limit_per_minute,
                          settings.rate_limit_window_seconds)
    cache = DisputeCache(redis, settings.cache_ttl_seconds, settings.cache_namespace)

    def rate_limit(request: Request, response: Response) -> None:
        """Per-client limit on POST endpoints (api/controls.py); 429 over the limit."""
        if not limiter.enabled:
            return
        client = client_id(request, trust_header=settings.rate_limit_trust_client_id)
        decision = limiter.check(client)
        headers = {"X-RateLimit-Limit": str(decision.limit),
                   "X-RateLimit-Remaining": str(decision.remaining)}
        if not decision.allowed:
            log.warning("rate_limited", client=client)
            raise HTTPException(
                status_code=429, detail="rate limit exceeded: too many requests from this "
                f"client, retry in {decision.retry_after} s",
                headers={**headers, "Retry-After": str(decision.retry_after)},
            )
        response.headers.update(headers)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db(engine)
        log.info("startup", queue=settings.queue_name)
        yield

    app = FastAPI(
        title="Card Dispute Triage Service",
        version="0.1.0",
        description="Accepts dispute reports and returns cited liability recommendations. "
        "It never issues a credit or closes a dispute; a disputes analyst decides.",
        lifespan=lifespan,
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.state.queue = queue
    setup_observability(app)  # tracing and queue metrics (docs/observability.md)

    def quarantine(request: Request, raw: str, errors: list[dict[str, Any]]) -> int:
        with session_factory.begin() as session:
            row = Quarantine(
                correlation_id=request.state.correlation_id,
                endpoint=request.url.path,
                raw_payload=raw,
                errors=errors,
            )
            session.add(row)
            session.flush()
            row_id = row.id
        DISPUTES_QUARANTINED.inc()
        log.warning("request_quarantined", quarantine_id=row_id, error_count=len(errors))
        return row_id

    @app.post(
        "/disputes",
        status_code=202,
        response_model=DisputeAccepted,
        responses={
            422: {"description": "Rejected and quarantined, with field-level errors"},
            429: {"description": "Rate limit exceeded for this client; see Retry-After"},
        },
        dependencies=[Depends(rate_limit)],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/DisputeRequest"}
                    }
                },
            }
        },
    )
    async def submit_dispute(request: Request, response: Response):
        """Validate a dispute against the data contract; enqueue it or quarantine it.

        An identical dispute submitted again within CACHE_TTL_SECONDS gets the first run's
        id back (X-Cache: HIT) instead of a new run."""
        body = await request.body()
        raw = body.decode("utf-8", errors="replace")
        try:
            dispute = DisputeRequest.model_validate_json(body)
        except ValidationError as exc:
            errors = _validation_errors(exc)
            quarantine_id = quarantine(request, raw, errors)
            return JSONResponse(
                status_code=422, content={"detail": errors, "quarantine_id": quarantine_id}
            )

        _count_guardrail_flags(dispute)
        run_id = str(uuid.uuid4())
        key, cache_header = None, "BYPASS"
        if cache.enabled:
            key = dispute_key(dispute, cache.namespace)
            claimed, existing = cache.claim(key, run_id)
            if not claimed and existing is None:  # Redis unavailable: no caching this time
                key = None
            elif existing is not None:
                with session_factory() as session:
                    cached = session.get(Run, existing)
                    status = cached.status if cached else None
                    result_json = cached.result_json if cached else None
                if reusable(status, result_json):
                    CACHE_REQUESTS.labels("hit").inc()
                    request.state.run_id = existing
                    response.headers["X-Cache"] = "HIT"
                    log.info("dispute_cache_hit", run_id=existing, cached_status=status)
                    return DisputeAccepted(run_id=existing, status=status,
                                           status_url=f"/runs/{existing}")
                CACHE_REQUESTS.labels("stale").inc()
                cache.take_over(key, run_id)
                cache_header = "MISS"
            else:
                CACHE_REQUESTS.labels("miss").inc()
                cache_header = "MISS"
        response.headers["X-Cache"] = cache_header

        structlog.contextvars.bind_contextvars(run_id=run_id)
        request.state.run_id = run_id
        correlation_id = request.state.correlation_id
        with session_factory.begin() as session:
            session.add(
                Run(
                    id=run_id,
                    status=RunStatus.queued,
                    correlation_id=correlation_id,
                    request_json=dispute.model_dump(mode="json"),
                )
            )
        try:
            queue.enqueue(
                JOB_FUNC,
                run_id,
                correlation_id,
                job_id=run_id,
                job_timeout=settings.run_timeout_seconds + RQ_TIMEOUT_GRACE_SECONDS,
                result_ttl=3600,
                failure_ttl=86400,
                meta=job_meta(),  # trace context for the worker
            )
        except Exception as exc:
            with session_factory.begin() as session:
                run = session.get(Run, run_id)
                run.status = RunStatus.failed
                run.error = f"enqueue failed: {exc}"
            if key:
                cache.release(key, run_id)
            log.exception("enqueue_failed")
            raise HTTPException(status_code=503, detail="queue unavailable") from exc

        DISPUTES_ACCEPTED.inc()
        log.info("run_enqueued")
        return DisputeAccepted(
            run_id=run_id, status=RunStatus.queued, status_url=f"/runs/{run_id}"
        )

    @app.get("/runs/{run_id}", response_model=RunView)
    def get_run(run_id: str):
        with session_factory() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise HTTPException(status_code=404, detail="run not found")
            result = RunResult.model_validate(run.result_json) if run.result_json else None
            return RunView(
                run_id=run.id,
                status=run.status,
                created_at=run.created_at,
                updated_at=run.updated_at,
                result=result,
                error=run.error,
            )

    @app.post("/runs/{run_id}/feedback", status_code=201, response_model=FeedbackAccepted,
              dependencies=[Depends(rate_limit)])
    def post_feedback(run_id: str, feedback: FeedbackRequest):
        with session_factory.begin() as session:
            if session.get(Run, run_id) is None:
                raise HTTPException(status_code=404, detail="run not found")
            row = Feedback(
                run_id=run_id, satisfactory=feedback.satisfactory, reason=feedback.reason
            )
            session.add(row)
            session.flush()
            feedback_id = row.id
        log.info("feedback_recorded", run_id=run_id, satisfactory=feedback.satisfactory)
        return FeedbackAccepted(feedback_id=feedback_id, run_id=run_id)

    @app.get("/quarantine")
    def list_quarantine(limit: int = Query(50, ge=1, le=500)):
        with session_factory() as session:
            rows = session.scalars(
                select(Quarantine).order_by(Quarantine.id.desc()).limit(limit)
            ).all()
            return [
                {
                    "id": r.id,
                    "received_at": r.received_at,
                    "correlation_id": r.correlation_id,
                    "endpoint": r.endpoint,
                    "raw_payload": r.raw_payload,
                    "errors": r.errors,
                }
                for r in rows
            ]

    @app.get("/quality")
    def quality(days: int = Query(7, ge=1, le=90),
                bucket: Literal["hour", "day"] = Query("day")):
        """Live quality over time (L6): LLM-judge scores of sampled runs, per bucket, with
        analyst feedback. Written by the quality scorer (python -m quality run)."""
        if bucket == "hour" and days > 14:
            raise HTTPException(status_code=422, detail="bucket=hour allows at most 14 days")
        return summarize(session_factory, days=days, bucket=bucket)

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz():
        checks: dict[str, str] = {}
        try:
            redis.ping()
            checks["redis"] = "ok"
        except Exception as exc:
            checks["redis"] = f"error: {exc.__class__.__name__}"
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["postgres"] = "ok"
        except Exception as exc:
            checks["postgres"] = f"error: {exc.__class__.__name__}"
        ready = all(v == "ok" for v in checks.values())
        return JSONResponse(
            status_code=200 if ready else 503,
            content={"status": "ready" if ready else "not_ready", "checks": checks},
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    # Make DisputeRequest appear in the OpenAPI components (body is parsed manually above).
    def openapi_with_contract():
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        contract = DisputeRequest.model_json_schema(ref_template="#/components/schemas/{model}")
        components.update(contract.pop("$defs", {}))
        components["DisputeRequest"] = contract
        app.openapi_schema = schema
        return schema

    app.openapi = openapi_with_contract

    # Web client, mounted last so API routes take precedence.
    if settings.web_dir.is_dir():
        app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
    return app
