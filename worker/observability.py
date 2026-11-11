"""Worker tracing and metrics (docs/observability.md).

Tracing: each job runs in a span whose parent is the API request that created the run (the
context travels in the job's meta), so a run is one trace. The agents' node, tool and model
spans (agent/tracking.py) and the gateway's spans (via the httpx instrumentation) nest under
it. Spans are flushed at the end of the job: RQ's work horse exits with os._exit.

Metrics: RQ forks a work horse per job, and the horse exits after it, so in-memory metrics
would vanish with it. prometheus_client's multiprocess mode keeps them in files under
PROMETHEUS_MULTIPROC_DIR; the worker's main process serves them on WORKER_METRICS_PORT. By
default every process writes its own files (named by pid), which would add files with every
job. A worker runs one job at a time, so all work horses of a container share one identity
("job") and add to the same files; the main process is "main" (docs/decisions.md D-030).
The per-run metrics are derived from the run's trace list after the run, so they cover every
agent mode without code in the agents.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog
from opentelemetry.trace import SpanKind, Status, StatusCode
from prometheus_client import values

from common.tracing import context_from_meta, flush, get_tracer, set_attributes

log = structlog.get_logger("worker.observability")

_MAIN_PID = os.getpid()


def _process_identity() -> str:
    """Stable per container: one main process, at most one work horse at a time."""
    return "main" if os.getpid() == _MAIN_PID else "job"


# Must run before any metric below is created.
if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
    values.ValueClass = values.MultiProcessValue(process_identifier=_process_identity)

from prometheus_client import (  # noqa: E402  (after the value class is chosen)
    CollectorRegistry,
    Counter,
    Histogram,
    ProcessCollector,
    multiprocess,
    start_http_server,
)
from prometheus_client.core import GaugeMetricFamily  # noqa: E402

RUN_BUCKETS = (1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 300, 600)
STEP_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2, 4, 8, 15, 30, 60)
COUNT_BUCKETS = (0, 1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 30, 40)

RUNS = Counter("runs_finished_total", "Runs finished by the worker", ["status", "mode"])
RUN_END_TO_END = Histogram(
    "run_end_to_end_seconds", "Submission (runs.created_at) to final status: the SLO "
    "measure (docs/slo.md)", ["status"], buckets=RUN_BUCKETS)
RUN_QUEUE_WAIT = Histogram("run_queue_wait_seconds", "Submission to the worker starting the run",
                           buckets=RUN_BUCKETS)
RUN_PROCESSING = Histogram("run_processing_seconds", "Worker start to final status",
                           ["status"], buckets=RUN_BUCKETS)
WORKER_BUSY = Counter("worker_busy_seconds_total",
                      "Seconds spent processing runs; rate() is the worker's utilisation")

AGENT_TASKS = Counter("agent_tasks_total", "Agent node executions by outcome "
                      "(ok, escalated, halted, failed)", ["agent", "outcome"])
NODE_LATENCY = Histogram("agent_node_seconds", "Latency of one graph node", ["node"],
                         buckets=STEP_BUCKETS)
VERIFIER_DECISIONS = Counter("verifier_decisions_total", "Verifier decisions",
                             ["outcome", "action"])
TOOL_CALLS = Counter("agent_tool_calls_total", "Retrieval tool calls", ["tool", "outcome"])
TOOL_LATENCY = Histogram("agent_tool_call_seconds", "Latency of one tool call", ["tool"],
                         buckets=STEP_BUCKETS)
LLM_CALLS = Counter("agent_llm_calls_total", "Model calls made by the agents",
                    ["agent", "outcome"])
LLM_LATENCY = Histogram("agent_llm_call_seconds", "Latency of one model call as the agent "
                        "sees it (gateway queueing and retries included)", ["agent"],
                        buckets=STEP_BUCKETS)
RUN_STEPS = Histogram("agent_steps_per_run", "Graph steps (node executions) per run",
                      buckets=COUNT_BUCKETS)
RUN_TOOL_CALLS = Histogram("agent_tool_calls_per_run", "Tool calls per run",
                           buckets=COUNT_BUCKETS)
RUN_LLM_CALLS = Histogram("agent_llm_calls_per_run", "Model calls per run",
                          buckets=COUNT_BUCKETS)
GRAPH_RUNS = Counter("agent_graph_runs_total", "Graph runs by outcome and whether the "
                     "Investigator redrafted", ["outcome", "retried"])
VERIFY_RETRIES = Counter("agent_verify_retries_total", "Investigator redrafts asked for by "
                         "the Verifier")

_AGENT_OF_NODE = {"investigate": "investigator", "verify": "verifier"}
_TOOLS = {"search_corpus", "get_clause", "related"}


def _investigator_outcome(summary: str) -> str:
    """AGENT_MODE=investigator records "completed", "escalated: <reason>" or "failed"."""
    if summary == "completed":
        return "ok"
    return "escalated" if summary.startswith("escalated") else "failed"


def record_trace(trace: list[dict[str, Any]]) -> None:
    """Per-agent metrics from one run's trace (agent/tracking.py, agent/graph.py)."""
    for entry in trace:
        step = entry.get("step")
        seconds = entry.get("latency_ms", 0) / 1000
        if step == "node":
            NODE_LATENCY.labels(entry["node"]).observe(seconds)
            if agent := _AGENT_OF_NODE.get(entry["node"]):
                AGENT_TASKS.labels(agent, entry.get("outcome", "ok")).inc()
        elif step == "verifier_decision":
            VERIFIER_DECISIONS.labels(entry["outcome"], entry["action"]).inc()
        elif step in _TOOLS:
            TOOL_CALLS.labels(step, "error" if "error" in entry else "ok").inc()
            TOOL_LATENCY.labels(step).observe(seconds)
        elif step == "llm":
            LLM_CALLS.labels(entry["agent"], "error" if "error" in entry else "ok").inc()
            LLM_LATENCY.labels(entry["agent"]).observe(seconds)
        elif step == "graph":
            RUN_STEPS.observe(entry["steps"])
            RUN_TOOL_CALLS.observe(entry["tool_calls"])
            RUN_LLM_CALLS.observe(entry["llm_calls"])
            retries = max(entry["attempts"] - 1, 0)
            VERIFY_RETRIES.inc(retries)
            GRAPH_RUNS.labels(entry["outcome"], str(retries > 0).lower()).inc()
        elif step == "investigator":
            AGENT_TASKS.labels("investigator", _investigator_outcome(entry["outcome"])).inc()
            RUN_TOOL_CALLS.observe(entry["tool_calls"])
            RUN_LLM_CALLS.observe(entry["llm_calls"])


def record_run(status: str, mode: str, trace: list[dict[str, Any]] | None, *,
               queue_wait_s: float, processing_s: float, end_to_end_s: float) -> None:
    try:
        RUNS.labels(status, mode).inc()
        RUN_QUEUE_WAIT.observe(max(queue_wait_s, 0))
        RUN_PROCESSING.labels(status).observe(processing_s)
        RUN_END_TO_END.labels(status).observe(max(end_to_end_s, 0))
        WORKER_BUSY.inc(processing_s)
        if trace:
            record_trace(trace)
    except Exception:  # metrics must never fail a run
        log.exception("run_metrics_failed")


@contextmanager
def run_span(run_id: str, correlation_id: str, job_meta: dict | None) -> Iterator[Any]:
    """The job's span, a child of the API request that created the run. Flushed at the end."""
    try:
        with get_tracer().start_as_current_span(
                "worker.process_run", context=context_from_meta(job_meta), kind=SpanKind.CONSUMER,
                attributes={"step": "worker", "run_id": run_id,
                            "correlation_id": correlation_id}) as span:
            yield span
    finally:
        flush()


def end_run_span(span: Any, status: str, *, route: str | None = None,
                 error: str | None = None, **timings: float) -> None:
    set_attributes(span, status=status, route=route, error=error,
                   **{k: round(v, 3) for k, v in timings.items()})
    if error:
        span.set_status(Status(StatusCode.ERROR, error))


class _WorkerCollector:
    """Facts about this worker container, computed once at start: agent mode and whether
    the corpus index baked into the image is fresh (python -m ingest status)."""

    def __init__(self, agent_mode: str):
        self.agent_mode = agent_mode
        self.fresh, self.built_at, self.chunks = _corpus_freshness()

    def collect(self):
        info = GaugeMetricFamily("worker_info", "Worker configuration", labels=["agent_mode"])
        info.add_metric([self.agent_mode], 1)
        yield info
        if self.fresh is not None:
            yield GaugeMetricFamily("corpus_index_fresh", "1 if data/ matches the corpus and "
                                    "the ingest code, else 0", value=int(self.fresh))
        if self.built_at is not None:
            yield GaugeMetricFamily("corpus_index_built_timestamp_seconds",
                                    "When the corpus index was built", value=self.built_at)
        if self.chunks is not None:
            yield GaugeMetricFamily("corpus_chunks", "Chunks in the corpus index",
                                    value=self.chunks)


def _corpus_freshness() -> tuple[bool | None, float | None, int | None]:
    try:
        from datetime import datetime

        from ingest.build import DATA_DIR, read_manifest, stale_reasons

        manifest = read_manifest(DATA_DIR)
        if manifest is None:
            return False, None, None
        built = datetime.fromisoformat(manifest["built_at"]).timestamp()
        return not stale_reasons(DATA_DIR), built, manifest["chunks"]["total"]
    except Exception:
        log.warning("corpus_freshness_unavailable", exc_info=True)
        return None, None, None


def start_metrics_server(port: int, agent_mode: str) -> None:
    """Serve the work horses' metrics (multiprocess files) and the main process's own."""
    directory = os.environ.get("PROMETHEUS_MULTIPROC_DIR")
    if not directory:
        log.info("worker_metrics_disabled", reason="PROMETHEUS_MULTIPROC_DIR not set")
        return
    for stale in Path(directory).glob("*.db"):  # left by a previous container start
        stale.unlink()
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    ProcessCollector(registry=registry)
    registry.register(_WorkerCollector(agent_mode))
    start_http_server(port, registry=registry)
    log.info("worker_metrics_serving", port=port)
