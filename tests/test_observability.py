"""Tracing and metrics (docs/observability.md): one trace per run from the API through the
worker, the graph's nodes, tool and model calls, to the gateway; per-run metrics from the
trace list; the worker's multiprocess metrics; and the run reconstruction (make trace).
Spans go to an in-memory exporter; nothing is sent anywhere."""

import ast
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import fakeredis
import pytest
import structlog
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY
from rq import Queue
from sqlalchemy.orm import Session

from agent import runner
from common.db import LlmCall, Run, init_db, make_engine, make_session_factory
from common.tracing import get_tracer, setup_tracing, trace_id
from scripts.trace_run import reconstruct
from tests.test_graph import _graph
from tests.test_verifier import SUPPORTED
from worker import tasks
from worker.observability import record_trace
from worker.settings import WorkerSettings


@pytest.fixture(scope="module")
def exporter():
    memory = InMemorySpanExporter()
    # Whichever module configured tracing first, add the in-memory exporter to it.
    setup_tracing("test").add_span_processor(SimpleSpanProcessor(memory))
    return memory


@pytest.fixture
def spans(exporter):
    exporter.clear()
    yield exporter.get_finished_spans
    exporter.clear()


def _by_name(finished):
    out = {}
    for span in finished:
        out.setdefault(span.name, []).append(span)
    return out


def _tid(span) -> str:
    return format(span.context.trace_id, "032x")


# ---------- agent spans (agent/tracking.py, traced_node) ----------

def test_graph_run_is_one_trace_of_nodes_tools_and_model_calls(spans):
    from tests.test_graph import DRAFT, LOOKUP

    with structlog.contextvars.bound_contextvars(run_id="run-g", correlation_id="corr-g"), \
            get_tracer().start_as_current_span("worker.process_run") as root:
        result, _, _ = _graph([LOOKUP, DRAFT, SUPPORTED, {"conflict": False, "clause_ids": [],
                                                           "reason": "None."}])
    assert result.status == "completed"
    finished = spans()
    by_name = _by_name(finished)
    assert {"node.investigate", "node.verify", "node.finalize", "llm.investigator",
            "llm.verifier", "tool.get_clause", "tool.related", "tool.search_corpus"} \
        <= by_name.keys()
    assert {_tid(s) for s in finished} == {_tid(root)}
    for span in finished:
        assert span.attributes["run_id"] == "run-g"
        assert span.attributes["correlation_id"] == "corr-g"
        assert span.attributes["step"]

    nodes = {s.context.span_id: s.name for s in finished if s.name.startswith("node.")}
    for span in finished:
        if span.name.startswith(("tool.", "llm.")):
            assert nodes.get(span.parent.span_id) in ("node.investigate", "node.verify")

    llm = by_name["llm.investigator"][0].attributes
    assert (llm["step"], llm["prompt_version"], llm["outcome"]) == ("investigator", "v1", "ok")
    assert llm["prompt_tokens"] == 100 and llm["completion_tokens"] == 20
    assert "cost_usd" in llm and llm["model"] == "scripted"

    (verify,) = by_name["node.verify"]
    assert (verify.attributes["verifier_outcome"], verify.attributes["verifier_action"]) == (
        "verified", "return")
    assert verify.attributes["verifier_reason"] == "all claims supported"
    search = by_name["tool.search_corpus"][0].attributes
    assert search["query"] and "results" in search and search["latency_ms"] >= 0
    (finalize,) = by_name["node.finalize"]
    assert (finalize.attributes["status"], finalize.attributes["route"]) == (
        "completed", "provisional_credit")


def test_model_error_marks_the_llm_span(spans):
    from contracts.llm import LLMUnavailableError
    from tests.test_graph import LOOKUP

    result, _, _ = _graph([LOOKUP, LLMUnavailableError("down")])
    assert result.status == "escalated"
    by_name = _by_name(spans())
    failed = [s for s in by_name["llm.investigator"] if s.attributes["outcome"] == "error"]
    assert failed and not failed[0].status.is_ok
    (investigate,) = by_name["node.investigate"]
    assert investigate.attributes["outcome"] == "halted"
    assert "model unavailable" in investigate.attributes["halt_reason"]


# ---------- API -> job meta -> worker ----------

@pytest.fixture
def worker_env(monkeypatch, database_url):
    monkeypatch.setattr(runner, "FAKE_DELAY_SECONDS", 0)
    tasks.get_settings.cache_clear()
    tasks.get_session_factory.cache_clear()
    settings = WorkerSettings(database_url=database_url, redis_url="redis://unused")
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    yield settings
    tasks.get_session_factory.cache_clear()


def test_trace_context_travels_from_api_through_the_job_to_the_worker(
        spans, client, redis, engine, payload, worker_env, monkeypatch):
    res = client.post("/disputes", json=payload, headers={"X-Correlation-ID": "corr-api"})
    run_id = res.json()["run_id"]
    job = Queue("disputes", connection=redis).fetch_job(run_id)
    assert "traceparent" in job.meta["otel"]

    (post,) = [s for s in spans() if s.attributes.get("http.route") == "/disputes"
               and s.kind.name == "SERVER"]
    assert (post.attributes["run_id"], post.attributes["correlation_id"],
            post.attributes["step"]) == (run_id, "corr-api", "api")

    monkeypatch.setattr(tasks, "get_current_job", lambda: job)
    assert tasks.process_run(run_id, "corr-api") == "completed"

    (worker,) = [s for s in spans() if s.name == "worker.process_run"]
    assert _tid(worker) == _tid(post)
    assert worker.parent is not None
    assert (worker.attributes["status"], worker.attributes["step"]) == ("completed", "worker")
    assert worker.attributes["end_to_end_s"] >= worker.attributes["processing_s"] >= 0
    with Session(engine) as s:
        trace = s.get(Run, run_id).result_json["trace"]
    assert {"step": "otel_trace", "trace_id": _tid(post)} in trace


def test_polling_and_health_requests_are_not_traced(spans, client):
    client.get("/healthz")
    client.get("/runs/does-not-exist")
    assert not [s for s in spans() if s.kind.name == "SERVER"]


def test_queue_metrics_on_the_api(client, payload):
    client.post("/disputes", json=payload)
    text = client.get("/metrics").text
    assert 'rq_jobs{state="queued"} 1.0' in text
    assert 'rq_workers{state="busy"} 0.0' in text


# ---------- gateway ----------

def test_gateway_spans_join_the_callers_trace(spans, tmp_path):
    from llm_gateway.main import create_app
    from llm_gateway.settings import GatewaySettings

    engine = make_engine(f"sqlite:///{tmp_path / 'gw.db'}")
    init_db(engine)
    app = create_app(GatewaySettings(redis_url="redis://unused", database_url="sqlite://",
                                     llm_provider="stub", stub_delay_ms=0,
                                     llm_max_requests_per_minute=60000),
                     redis=fakeredis.FakeAsyncRedis(), engine=engine)
    parent = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
    body = {"prompt": "p", "prompt_version": "v1", "step": "verifier", "run_id": "run-gw"}
    assert TestClient(app).post("/v1/complete", json=body, headers={
        "traceparent": parent, "X-Correlation-ID": "corr-gw"}).status_code == 200

    by_name = _by_name(spans())
    (gateway,) = by_name["llm.gateway"]
    (provider,) = by_name["llm.provider.stub"]
    server = [s for s in spans() if s.kind.name == "SERVER"][0]
    for span in (gateway, provider, server):
        assert _tid(span) == "0af7651916cd43dd8448eb211c80319c"
        assert (span.attributes["run_id"], span.attributes["correlation_id"],
                span.attributes["step"]) == ("run-gw", "corr-gw", "verifier")
    assert gateway.attributes["outcome"] == "ok" and gateway.attributes["attempts"] == 1
    assert gateway.attributes["prompt_version"] == "v1"
    assert gateway.attributes["prompt_tokens"] >= 1 and "cost_usd" in gateway.attributes
    assert provider.parent.span_id == gateway.context.span_id


# ---------- per-run metrics from the trace list ----------

def _value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_run_metrics_are_derived_from_the_trace():
    trace = [
        {"step": "node", "node": "investigate", "outcome": "ok", "latency_ms": 1200},
        {"step": "search_corpus", "query": "q", "results": ["a"], "latency_ms": 40},
        {"step": "llm", "agent": "investigator", "latency_ms": 900},
        {"step": "llm", "agent": "verifier", "latency_ms": 10, "error": "LLMUnavailableError"},
        {"step": "node", "node": "verify", "outcome": "escalated", "latency_ms": 30},
        {"step": "verifier_decision", "outcome": "unsupported", "action": "retry"},
        {"step": "graph", "outcome": "escalated", "llm_calls": 2, "tool_calls": 1,
         "steps": 3, "attempts": 2},
    ]
    before = {
        "inv_ok": _value("agent_tasks_total", agent="investigator", outcome="ok"),
        "ver_esc": _value("agent_tasks_total", agent="verifier", outcome="escalated"),
        "llm_err": _value("agent_llm_calls_total", agent="verifier", outcome="error"),
        "retries": _value("agent_verify_retries_total"),
        "retried": _value("agent_graph_runs_total", outcome="escalated", retried="true"),
        "decision": _value("verifier_decisions_total", outcome="unsupported", action="retry"),
        "tools": _value("agent_tool_calls_total", tool="search_corpus", outcome="ok"),
        "steps": _value("agent_steps_per_run_sum"),
    }
    record_trace(trace)
    assert _value("agent_tasks_total", agent="investigator", outcome="ok") == before["inv_ok"] + 1
    assert _value("agent_tasks_total", agent="verifier",
                  outcome="escalated") == before["ver_esc"] + 1
    assert _value("agent_llm_calls_total", agent="verifier",
                  outcome="error") == before["llm_err"] + 1
    assert _value("agent_verify_retries_total") == before["retries"] + 1
    assert _value("agent_graph_runs_total", outcome="escalated",
                  retried="true") == before["retried"] + 1
    assert _value("verifier_decisions_total", outcome="unsupported",
                  action="retry") == before["decision"] + 1
    assert _value("agent_tool_calls_total", tool="search_corpus",
                  outcome="ok") == before["tools"] + 1
    assert _value("agent_steps_per_run_sum") == before["steps"] + 3


def test_worker_run_records_end_to_end_metrics(worker_env, engine, payload):
    run_id = "run-metrics"
    created = datetime.now(UTC) - timedelta(seconds=3)
    with Session(engine) as s, s.begin():
        s.add(Run(id=run_id, status="queued", correlation_id="c", request_json=payload,
                  created_at=created))
    count = _value("run_end_to_end_seconds_count", status="completed")
    tasks.process_run(run_id, "c")
    assert _value("run_end_to_end_seconds_count", status="completed") == count + 1
    assert _value("runs_finished_total", status="completed", mode="fake") >= 1
    assert _value("worker_busy_seconds_total") > 0


def test_forked_work_horses_share_one_set_of_metric_files(tmp_path):
    """D-030: every work horse of a container adds to the same files, and their counts
    survive the horse's exit."""
    code = textwrap.dedent(f"""
        import os
        os.environ["PROMETHEUS_MULTIPROC_DIR"] = {str(tmp_path)!r}
        from worker.observability import RUNS
        from prometheus_client import CollectorRegistry, multiprocess
        for _ in range(3):
            pid = os.fork()
            if pid == 0:
                RUNS.labels("completed", "graph").inc()
                os._exit(0)
            os.waitpid(pid, 0)
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
        print(registry.get_sample_value("runs_finished_total",
                                        {{"status": "completed", "mode": "graph"}}))
        print(sorted(os.listdir({str(tmp_path)!r})))
    """)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, cwd=Path(__file__).parent.parent)
    total, files = out.stdout.strip().splitlines()
    assert float(total) == 3.0
    assert "counter_job.db" in files
    assert not [f for f in ast.literal_eval(files) if f.split("_")[-1].split(".")[0].isdigit()]


# ---------- make trace ----------

def test_reconstruct_prints_every_step_cost_and_the_jaeger_link(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    init_db(engine)
    tid = "0af7651916cd43dd8448eb211c80319c"
    trace = [
        {"step": "node", "node": "investigate", "attempt": 1, "outcome": "ok",
         "started_at": "t0", "ended_at": "t1", "latency_ms": 5100},
        {"step": "search_corpus", "query": "OTP shared with caller", "k": 5,
         "results": ["IDBI_POLICY:5.12", "RBI_2017_CIRCULAR:7(i)"], "latency_ms": 41},
        {"step": "llm", "agent": "investigator", "prompt_version": "v1", "model": "stub",
         "prompt_tokens": 900, "completion_tokens": 80, "cost_usd": 0.0012, "latency_ms": 4010},
        {"step": "verifier_decision", "outcome": "contested_fault", "action": "escalate",
         "reason": "the customer says they shared an OTP", "supported": 0, "unsupported": [],
         "modifiers": []},
        {"step": "otel_trace", "trace_id": tid},
    ]
    now = datetime.now(UTC)
    with Session(engine) as s, s.begin():
        s.add(Run(id="run-r", status="escalated", correlation_id="corr-r",
                  request_json={"channel": "UPI", "complaint_text": "A caller asked for my OTP."},
                  result_json={"route": "escalate", "prompt_version": "inv-v1+ver-v1",
                               "trace": trace}, created_at=now - timedelta(seconds=9),
                  updated_at=now))
        s.add(LlmCall(run_id="run-r", correlation_id="corr-r", step="investigator",
                      prompt_version="v1", provider="stub", model="stub", prompt_tokens=900,
                      completion_tokens=80, cost_usd=0.0012, latency_ms=4005, attempts=1,
                      outcome="ok"))
    text = reconstruct(make_session_factory(engine), "run-r", jaeger_api=None)
    for expected in ("status escalated", "OTP shared with caller", "IDBI_POLICY:5.12",
                     "prompt v1", "900 in / 80 out", "$0.001200", "contested_fault",
                     "action escalate", "node investigate", "model call: investigator",
                     f"http://localhost:16686/trace/{tid}", "attempts 1"):
        assert expected in text, expected
    with pytest.raises(LookupError):
        reconstruct(make_session_factory(engine), "missing", jaeger_api=None)


def test_trace_id_of_the_current_span():
    assert trace_id() is None
    with get_tracer().start_as_current_span("x") as span:
        assert trace_id() == _tid(span)
