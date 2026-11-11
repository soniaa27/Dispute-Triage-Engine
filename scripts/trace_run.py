"""Reconstruct one run from Postgres and its trace:  make trace RUN=<run_id>

Prints the run's request and result, then every step in order from the run's trace list:
graph nodes with their latency, each tool call with its query and the passage ids it
returned, each model call with prompt version, tokens, cost and latency, and the Verifier's
outcome, action and reason. Then per-step totals, the gateway's own record of each model call
(llm_calls), and the run's OpenTelemetry trace id with its Jaeger link, plus a summary of
the spans Jaeger holds for it.

Runs in the llm-gateway container (it reaches Postgres and Jaeger):
    docker compose exec llm-gateway python scripts/trace_run.py <run_id>
"""

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from common.db import LlmCall, Run, make_engine, make_session_factory  # noqa: E402

RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
JAEGER_API = os.environ.get("JAEGER_API_URL", "http://jaeger:16686")
JAEGER_UI = os.environ.get("JAEGER_UI_URL", "http://localhost:16686")
TEXT_WIDTH = 72


def _short(text: Any, width: int = TEXT_WIDTH) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[:width - 3] + "..."


def _ms(value: Any) -> str:
    return f"{value:>7} ms" if isinstance(value, int | float) else " " * 10


def _cost(value: Any) -> str:
    return f"${value:.6f}" if isinstance(value, int | float) else ""


def _entry_line(entry: dict[str, Any]) -> list[str]:
    step = entry.get("step")
    error = f"  ERROR {entry['error']}" if entry.get("error") else ""
    if step == "node":
        return [f"{_ms(entry.get('latency_ms'))}  node {entry.get('node')} "
                f"(attempt {entry.get('attempt', 1)}) {entry.get('outcome', '')}"
                f"  {entry.get('started_at', '')} -> {entry.get('ended_at', '')}{error}"]
    if step == "search_corpus":
        lines = [f"{_ms(entry.get('latency_ms'))}    search_corpus k={entry.get('k')} "
                 f"\"{_short(entry.get('query', ''), 60)}\"{error}"]
        results = entry.get("results") or []
        lines.append(" " * 16 + "-> " + (", ".join(results) if results else "no passages"))
        return lines
    if step == "get_clause":
        found = "found" if entry.get("found") else "not found"
        return [f"{_ms(entry.get('latency_ms'))}    get_clause {entry.get('clause')} -> {found}"
                f"{error}"]
    if step == "related":
        links = entry.get("results") or []
        return [f"{_ms(entry.get('latency_ms'))}    related {entry.get('clause')} -> "
                f"{', '.join(links) if links else 'no links'}{error}"]
    if step == "llm":
        tokens = (f"{entry.get('prompt_tokens', '?')} in / {entry.get('completion_tokens', '?')}"
                  " out tokens") if "prompt_tokens" in entry else ""
        return [f"{_ms(entry.get('latency_ms'))}    model call {entry.get('agent')} "
                f"prompt {entry.get('prompt_version')} {entry.get('model', '')} {tokens} "
                f"{_cost(entry.get('cost_usd'))}{error}"]
    if step == "investigator_draft":
        return [" " * 14 + f"draft: {entry.get('fraud_type')}, fault {entry.get('fault')}, "
                f"route {entry.get('route')}, cites {', '.join(entry.get('cited') or [])}"]
    if step == "verifier_decision":
        lines = [" " * 14 + f"verifier: outcome {entry.get('outcome')}, action "
                 f"{entry.get('action')}, {entry.get('supported', 0)} claims supported"]
        lines.append(" " * 16 + f"reason: {_short(entry.get('reason', ''))}")
        for claim in entry.get("unsupported") or []:
            lines.append(" " * 16 + f"unsupported: {_short(claim)}")
        if entry.get("modifiers"):
            lines.append(" " * 16 + f"modifying provisions: {', '.join(entry['modifiers'])}")
        return lines
    if step == "graph":
        return [" " * 14 + f"graph: {entry.get('outcome')} after {entry.get('steps')} steps, "
                f"{entry.get('llm_calls')} model calls, {entry.get('tool_calls')} tool calls, "
                f"{entry.get('attempts')} attempt(s), {entry.get('elapsed_s')} s"]
    if step == "otel_trace":
        return []
    other = {k: v for k, v in entry.items() if k not in ("step", "latency_ms")}
    return [f"{_ms(entry.get('latency_ms'))}  {step} {_short(other)}"]


def _totals(trace: list[dict[str, Any]]) -> list[str]:
    """Latency and cost per step: model calls by agent, tool calls by tool, nodes by name."""
    rows: dict[str, list[float]] = defaultdict(lambda: [0, 0.0, 0.0])
    for entry in trace:
        step = entry.get("step")
        if step == "llm":
            key = f"model call: {entry.get('agent')}"
        elif step in ("search_corpus", "get_clause", "related"):
            key = f"tool: {step}"
        elif step == "node":
            key = f"node: {entry.get('node')}"
        else:
            continue
        row = rows[key]
        row[0] += 1
        row[1] += entry.get("latency_ms", 0) or 0
        row[2] += entry.get("cost_usd", 0) or 0
    lines = [f"  {'step':<30} {'calls':>5} {'latency':>11} {'cost':>12}"]
    for key, (calls, latency, cost) in rows.items():
        lines.append(f"  {key:<30} {calls:>5} {latency / 1000:>9.2f} s {_cost(cost):>12}")
    return lines


def _jaeger_summary(trace_id: str, api: str) -> list[str]:
    try:
        res = httpx.get(f"{api.rstrip('/')}/api/traces/{trace_id}", timeout=5)
        res.raise_for_status()
        data = res.json().get("data") or []
    except Exception as exc:
        return [f"  Jaeger not reachable at {api}: {exc.__class__.__name__}"]
    if not data:
        return ["  Jaeger has no spans for this trace (expired, or Jaeger restarted)."]
    spans = data[0]["spans"]
    processes = data[0].get("processes", {})
    services = Counter(processes.get(s["processID"], {}).get("serviceName", "?") for s in spans)
    lines = [f"  {len(spans)} spans: " + ", ".join(f"{n} {s}" for s, n in services.items())]
    names = Counter(s["operationName"] for s in spans)
    lines.append("  " + ", ".join(f"{name} x{n}" for name, n in sorted(names.items())))
    return lines


def reconstruct(sessions, run_id: str, *, jaeger_api: str | None = JAEGER_API,
                jaeger_ui: str = JAEGER_UI) -> str:
    with sessions() as session:
        run = session.get(Run, run_id)
        if run is None:
            raise LookupError(f"run {run_id} not found")
        calls = session.scalars(select(LlmCall).where(LlmCall.run_id == run_id)
                                .order_by(LlmCall.id)).all()
    result = run.result_json or {}
    trace = result.get("trace") or []
    out = [f"Run {run.id}", f"  status {run.status}, route {result.get('route', '-')}, "
           f"prompt version {result.get('prompt_version', '-')}",
           f"  correlation id {run.correlation_id}",
           f"  created {run.created_at}, updated {run.updated_at} "
           f"({(run.updated_at - run.created_at).total_seconds():.1f} s)"]
    req = run.request_json or {}
    out.append(f"  request: {req.get('channel')}, Rs {req.get('transaction_amount_inr')}, "
               f"reported {req.get('reported_datetime')}, claims {req.get('customer_claims')}")
    out.append(f"  complaint: {_short(req.get('complaint_text', ''), 100)}")
    if result.get("escalation_reason"):
        out.append(f"  escalation reason: {_short(result['escalation_reason'], 100)}")
    if run.error:
        out.append(f"  error: {run.error}")
    if result.get("citations"):
        out.append("  citations: " + ", ".join(f"{c['doc_id']}:{c['clause_id']}"
                                                for c in result["citations"]))

    out += ["", "Steps (from the run's trace list)"]
    for entry in trace:
        out += _entry_line(entry)
    if not trace:
        out.append("  no trace recorded (the run failed before the agent returned)")

    out += ["", "Latency and cost per step"] + _totals(trace)

    out += ["", "Gateway record of each model call (llm_calls)"]
    for call in calls:
        out.append(f"  #{call.id} {call.step:<12} {call.outcome:<12} attempts {call.attempts} "
                   f"{call.latency_ms:>8.0f} ms  {call.prompt_tokens} in / "
                   f"{call.completion_tokens} out  {_cost(call.cost_usd)}  "
                   f"{call.provider}/{call.model} prompt {call.prompt_version}")
    if not calls:
        out.append("  none")
    total = sum(c.cost_usd for c in calls)
    out.append(f"  total {len(calls)} calls, {_cost(total)}")

    trace_id = next((e["trace_id"] for e in trace if e.get("step") == "otel_trace"), None)
    out += ["", "Trace"]
    if trace_id:
        out.append(f"  trace id {trace_id}")
        out.append(f"  Jaeger: {jaeger_ui.rstrip('/')}/trace/{trace_id}")
        if jaeger_api:
            out += _jaeger_summary(trace_id, jaeger_api)
    else:
        out.append("  no trace id recorded (run failed, or tracing was off)")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("run_id")
    parser.add_argument("--no-jaeger", action="store_true", help="do not query Jaeger")
    args = parser.parse_args(argv)
    if not RUN_ID.match(args.run_id):
        print(f"not a run id: {args.run_id!r}", file=sys.stderr)
        return 2
    sessions = make_session_factory(make_engine(os.environ["DATABASE_URL"]))
    try:
        print(reconstruct(sessions, args.run_id,
                          jaeger_api=None if args.no_jaeger else JAEGER_API))
    except LookupError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
