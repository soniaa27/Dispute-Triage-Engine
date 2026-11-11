"""Counting and tracing wrappers around the Retriever and the LLM client.

One TracedRetriever and one TracedLLM are made per run and handed to every agent in it, so
the Investigator and the Verifier share one tool-call budget (AGENT_MAX_TOOL_CALLS) and one
trace. Agents never count or trace their own calls.

The same wrappers emit OpenTelemetry spans (docs/observability.md): one per tool call and
per model call, under the span of the graph node that made it (traced_node).
"""

import functools
import math
import time
from collections.abc import Callable, Sequence
from typing import Any

from opentelemetry.trace import Status, StatusCode

from agent.errors import ToolBudgetExceeded
from common.tracing import get_tracer, set_attributes
from contracts.agent import DocId, Passage
from contracts.llm import LLMClient, LLMResponse
from contracts.tools import Retriever


def ms_since(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def remaining_tool_calls(retriever: Retriever) -> float:
    """Calls left before the wrapper raises ToolBudgetExceeded; unlimited for a retriever
    that is not wrapped (e.g. in a unit test)."""
    return getattr(retriever, "remaining", math.inf)


class TracedRetriever:
    """Counts every call against the run's tool-call limit, raising ToolBudgetExceeded
    instead of making the call over it, and records each call in the trace with its
    latency."""

    def __init__(self, inner: Retriever, trace: list[dict[str, Any]], max_calls: int):
        self._inner = inner
        self._trace = trace
        self._max_calls = max_calls
        self.calls = 0

    @property
    def remaining(self) -> int:
        return max(self._max_calls - self.calls, 0)

    def _call(self, entry: dict[str, Any], call: Callable[[], Any],
              results: Callable[[Any], dict[str, Any]]) -> Any:
        """Make the call; `results` turns its return value into trace fields."""
        if self.calls >= self._max_calls:
            raise ToolBudgetExceeded(f"tool-call limit of {self._max_calls} reached")
        self.calls += 1
        started = time.perf_counter()
        with get_tracer().start_as_current_span(
                f"tool.{entry['step']}", attributes={"step": entry["step"]}) as span:
            try:
                value = call()
                entry.update(results(value))
                return value
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
                span.set_status(Status(StatusCode.ERROR, entry["error"]))
                raise
            finally:
                entry["latency_ms"] = ms_since(started)
                self._trace.append(entry)
                set_attributes(span, tool_call=self.calls, **{
                    k: v for k, v in entry.items() if k not in ("step", "error")})

    def search_corpus(self, query: str, k: int = 5,
                      doc_ids: Sequence[DocId] | None = None) -> list[Passage]:
        entry: dict[str, Any] = {"step": "search_corpus", "query": query, "k": k}
        if doc_ids:
            entry["doc_ids"] = [str(d) for d in doc_ids]
        return self._call(entry, lambda: self._inner.search_corpus(query, k=k, doc_ids=doc_ids),
                          lambda found: {"results": [p.citation_id for p in found]})

    def get_clause(self, doc_id: DocId, clause_id: str) -> Passage | None:
        entry: dict[str, Any] = {"step": "get_clause", "clause": f"{doc_id}:{clause_id}"}
        return self._call(entry, lambda: self._inner.get_clause(doc_id, clause_id),
                          lambda passage: {"found": passage is not None})

    def related(self, doc_id: DocId, clause_id: str) -> list[str]:
        entry: dict[str, Any] = {"step": "related", "clause": f"{doc_id}:{clause_id}"}
        return self._call(entry, lambda: self._inner.related(doc_id, clause_id),
                          lambda links: {"results": links})


class TracedLLM:
    """Records each model call in the trace (tokens, cost, latency, or the error)."""

    def __init__(self, inner: LLMClient, trace: list[dict[str, Any]]):
        self._inner = inner
        self._trace = trace
        self.calls = 0

    def complete(self, prompt: str, *, prompt_version: str, step: str, run_id: str,
                 max_tokens: int | None = None) -> LLMResponse:
        self.calls += 1
        entry: dict[str, Any] = {"step": "llm", "agent": step, "prompt_version": prompt_version}
        started = time.perf_counter()
        with get_tracer().start_as_current_span(f"llm.{step}", attributes={
                "step": step, "run_id": run_id, "prompt_version": prompt_version,
                "llm_call": self.calls}) as span:
            try:
                response = self._inner.complete(prompt, prompt_version=prompt_version,
                                                step=step, run_id=run_id,
                                                max_tokens=max_tokens)
            except Exception as exc:
                entry["error"] = f"{type(exc).__name__}: {exc}"
                span.set_status(Status(StatusCode.ERROR, entry["error"]))
                set_attributes(span, outcome="error", error=entry["error"])
                raise
            finally:
                entry["latency_ms"] = ms_since(started)
                self._trace.append(entry)
            entry.update(model=response.model, prompt_tokens=response.prompt_tokens,
                         completion_tokens=response.completion_tokens,
                         cost_usd=response.cost_usd)
            set_attributes(span, outcome="ok", model=response.model,
                           prompt_tokens=response.prompt_tokens,
                           completion_tokens=response.completion_tokens,
                           cost_usd=response.cost_usd, latency_ms=entry["latency_ms"])
        return response


def traced_node(name: str, node: Callable[[Any], dict[str, Any]]) -> Callable[[Any], dict]:
    """Wraps a graph node in a span. Its tool and model calls become child spans; the
    span records the node's halt reason, and for the Verifier its outcome and action."""

    @functools.wraps(node)
    def run(state: Any) -> dict[str, Any]:
        with get_tracer().start_as_current_span(f"node.{name}", attributes={
                "step": name, "run_id": state.run_id, "attempt": state.attempts + 1}) as span:
            update = node(state)
            if update.get("halt_reason"):
                set_attributes(span, outcome="halted", halt_reason=update["halt_reason"])
            if (verdict := update.get("verifier")) is not None:
                set_attributes(span, verifier_outcome=verdict.outcome,
                               verifier_action=verdict.action,
                               verifier_reason=verdict.escalation_reason
                               or verdict.retry_strategy or "all claims supported",
                               unsupported_claims=len(verdict.unsupported_claims))
            if (result := update.get("result")) is not None:
                set_attributes(span, status=result.status, route=result.route)
            return update

    return run
