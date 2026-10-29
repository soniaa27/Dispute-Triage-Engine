"""Counting and tracing wrappers around the Retriever and the LLM client.

One TracedRetriever and one TracedLLM are made per run and handed to every agent in it, so
the Investigator and the Verifier share one tool-call budget (AGENT_MAX_TOOL_CALLS) and one
trace. Agents never count or trace their own calls.
"""

import math
import time
from collections.abc import Callable, Sequence
from typing import Any

from agent.errors import ToolBudgetExceeded
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

    def _call(self, entry: dict[str, Any], call: Callable[[], Any]) -> Any:
        if self.calls >= self._max_calls:
            raise ToolBudgetExceeded(f"tool-call limit of {self._max_calls} reached")
        self.calls += 1
        started = time.perf_counter()
        try:
            return call()
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["latency_ms"] = ms_since(started)
            self._trace.append(entry)

    def search_corpus(self, query: str, k: int = 5,
                      doc_ids: Sequence[DocId] | None = None) -> list[Passage]:
        entry: dict[str, Any] = {"step": "search_corpus", "query": query, "k": k}
        if doc_ids:
            entry["doc_ids"] = [str(d) for d in doc_ids]
        found = self._call(entry, lambda: self._inner.search_corpus(query, k=k, doc_ids=doc_ids))
        entry["results"] = [p.citation_id for p in found]
        return found

    def get_clause(self, doc_id: DocId, clause_id: str) -> Passage | None:
        entry: dict[str, Any] = {"step": "get_clause", "clause": f"{doc_id}:{clause_id}"}
        passage = self._call(entry, lambda: self._inner.get_clause(doc_id, clause_id))
        entry["found"] = passage is not None
        return passage

    def related(self, doc_id: DocId, clause_id: str) -> list[str]:
        entry: dict[str, Any] = {"step": "related", "clause": f"{doc_id}:{clause_id}"}
        links = self._call(entry, lambda: self._inner.related(doc_id, clause_id))
        entry["results"] = links
        return links


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
        try:
            response = self._inner.complete(prompt, prompt_version=prompt_version, step=step,
                                            run_id=run_id, max_tokens=max_tokens)
        except Exception as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["latency_ms"] = ms_since(started)
            self._trace.append(entry)
        entry.update(model=response.model, prompt_tokens=response.prompt_tokens,
                     completion_tokens=response.completion_tokens, cost_usd=response.cost_usd)
        return response
