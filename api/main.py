"""FastAPI app: dispute ingestion, run status, quarantine, feedback, health, metrics.

Run with:  uvicorn --factory api.main:create_app
"""

import json
import uuid
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError
from redis import Redis
from rq import Queue
from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from api.metrics import DISPUTES_ACCEPTED, DISPUTES_QUARANTINED
from api.middleware import CorrelationIdMiddleware
from api.settings import ApiSettings
from common.db import Feedback, Quarantine, Run, init_db, make_engine, make_session_factory
from common.logging import configure_logging
from contracts import (
    DisputeAccepted,
    DisputeRequest,
    FeedbackAccepted,
    FeedbackRequest,
    RunResult,
    RunStatus,
    RunView,
)

# Job function path; the API never imports worker/agent code.
JOB_FUNC = "worker.tasks.process_run"
# Hard backstop for RQ beyond the worker's own wall-clock timeout.
RQ_TIMEOUT_GRACE_SECONDS = 30

log = structlog.get_logger("api")


def _validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    # Round-trip through JSON so every error (including ctx values) is serialisable.
    return json.loads(exc.json(include_url=False))


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
        responses={422: {"description": "Rejected and quarantined, with field-level errors"}},
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
    async def submit_dispute(request: Request):
        """Validate a dispute against the data contract; enqueue it or quarantine it."""
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

        run_id = str(uuid.uuid4())
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
            )
        except Exception as exc:
            with session_factory.begin() as session:
                run = session.get(Run, run_id)
                run.status = RunStatus.failed
                run.error = f"enqueue failed: {exc}"
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

    @app.post("/runs/{run_id}/feedback", status_code=201, response_model=FeedbackAccepted)
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
