"""API tracing and queue metrics (docs/observability.md). Called once from create_app.

- Every request gets a server span (FastAPI instrumentation). POST /disputes is the root of
  the run's trace: the job carries its context (common.tracing.job_meta) to the worker.
- The span gets run_id, correlation_id and step "api" once the handler has set them.
- /metrics also reports the RQ queue: jobs waiting, running and failed, and workers busy
  and idle (the load test's first bottleneck signals).
"""

from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from prometheus_client import REGISTRY
from prometheus_client.core import GaugeMetricFamily
from rq import Queue, Worker
from rq.registry import FailedJobRegistry, StartedJobRegistry

from common.tracing import set_attributes, setup_tracing

# Polling, health and scrape requests would bury the run traces.
EXCLUDED_URLS = "/healthz,/readyz,/metrics,/runs/.*,/quarantine,/quality"


class _RunAttributes:
    """ASGI middleware inside the server span: copies what the handlers put on
    request.state onto it when the response starts (the span ends with the response)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        span = trace.get_current_span()

        async def send_with_attributes(message):
            if message["type"] == "http.response.start":
                state = scope.get("state") or {}
                set_attributes(span, step="api", run_id=state.get("run_id"),
                               correlation_id=state.get("correlation_id"))
            await send(message)

        await self.app(scope, receive, send_with_attributes)


class QueueCollector:
    """Queue depth and worker states, read from Redis at scrape time."""

    def __init__(self):
        self.queue: Queue | None = None

    def collect(self):
        if self.queue is None:
            return
        jobs = GaugeMetricFamily("rq_jobs", "RQ jobs by state", labels=["state"])
        workers = GaugeMetricFamily("rq_workers", "RQ workers by state", labels=["state"])
        try:
            conn = self.queue.connection
            jobs.add_metric(["queued"], self.queue.count)
            jobs.add_metric(["started"], StartedJobRegistry(queue=self.queue).count)
            jobs.add_metric(["failed"], FailedJobRegistry(queue=self.queue).count)
            states = [w.get_state() for w in Worker.all(connection=conn)]
            workers.add_metric(["busy"], sum(s == "busy" for s in states))
            workers.add_metric(["idle"], sum(s != "busy" for s in states))
        except Exception:  # metrics must still render if Redis is down
            return
        yield jobs
        yield workers


_queue_collector = QueueCollector()
REGISTRY.register(_queue_collector)


def setup_observability(app) -> None:
    setup_tracing("api")
    _queue_collector.queue = app.state.queue
    # Added before the instrumentation so that it runs inside the server span.
    app.add_middleware(_RunAttributes)
    FastAPIInstrumentor.instrument_app(app, excluded_urls=EXCLUDED_URLS,
                                       exclude_spans=["receive", "send"])
