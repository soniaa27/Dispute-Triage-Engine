"""Test doubles for the agents: a scripted LLM, in-memory retrievers and scenario requests.
Used only by tests; no model is ever called."""

import json
import time
from pathlib import Path
from typing import Any

from contracts import DisputeRequest
from contracts.agent import DocId, Passage
from contracts.llm import LLMResponse

RBI, IDBI = DocId.RBI_2017_CIRCULAR, DocId.IDBI_POLICY
SCENARIOS = Path(__file__).resolve().parent.parent / "eval" / "scenarios.jsonl"


def scenarios() -> dict[str, dict]:
    with SCENARIOS.open(encoding="utf-8") as f:
        return {s["id"]: s for s in map(json.loads, f) if s}


def scenario_request(scenario_id: str) -> DisputeRequest:
    return DisputeRequest.model_validate(scenarios()[scenario_id]["request"])


class ScriptedLLM:
    """Replies in order. A reply is text, a dict (sent as JSON) or an exception (raised).
    Records (step, prompt) for every call; `delay` simulates model latency."""

    def __init__(self, replies: list[Any], delay: float = 0.0):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.delay = delay

    @property
    def steps(self) -> list[str]:
        return [step for step, _ in self.calls]

    def complete(self, prompt, *, prompt_version, step, run_id, max_tokens=None):
        self.calls.append((step, prompt))
        if not self.replies:
            raise AssertionError(f"unscripted LLM call #{len(self.calls)} ({step})")
        if self.delay:
            time.sleep(self.delay)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = json.dumps(reply) if isinstance(reply, dict) else reply
        return LLMResponse(text=text, prompt_tokens=100, completion_tokens=20, cost_usd=0.0,
                           model="scripted", latency_ms=1, prompt_version=prompt_version,
                           step=step)


class DictRetriever:
    """Clauses by "DOC:clause", links by "DOC:clause", search results by query. Records
    every call in order."""

    def __init__(self, clauses: list[Passage], links: dict[str, list[str]] | None = None,
                 results: dict[str, list[str]] | None = None):
        self.clauses = {p.citation_id: p for p in clauses}
        self.links = links or {}
        self.results = results or {}
        self.calls: list[tuple] = []

    def search_corpus(self, query, k=5, doc_ids=None):
        self.calls.append(("search", query))
        return [self.clauses[c] for c in self.results.get(query, [])][:k]

    def get_clause(self, doc_id, clause_id):
        self.calls.append(("get_clause", f"{doc_id}:{clause_id}"))
        return self.clauses.get(f"{doc_id}:{clause_id}")

    def related(self, doc_id, clause_id):
        self.calls.append(("related", f"{doc_id}:{clause_id}"))
        return list(self.links.get(f"{doc_id}:{clause_id}", []))


class ScriptedSearch:
    """A real retriever (real chunk text, real links) whose searches return fixed clauses,
    so scenario tests do not depend on embedding quality."""

    def __init__(self, real, results: dict[str, list[tuple[DocId, str]]]):
        self.real = real
        self.results = results
        self.calls: list[tuple] = []

    def search_corpus(self, query, k=5, doc_ids=None):
        self.calls.append(("search", query))
        return [self.real.get_clause(d, c) for d, c in self.results.get(query, [])][:k]

    def get_clause(self, doc_id, clause_id):
        self.calls.append(("get_clause", f"{doc_id}:{clause_id}"))
        return self.real.get_clause(doc_id, clause_id)

    def related(self, doc_id, clause_id):
        self.calls.append(("related", f"{doc_id}:{clause_id}"))
        return self.real.related(doc_id, clause_id)


def passage(doc_id: DocId, clause_id: str, text: str | None = None) -> Passage:
    return Passage(doc_id=doc_id, clause_id=clause_id,
                   text=text or f"Text of {doc_id} clause {clause_id} for the test.")


# ---------- scripted Investigator replies (agent/prompts/v1.yaml format) ----------

def lookups(*ids: tuple[DocId, str]) -> dict:
    return {"action": "search", "searches": [],
            "lookups": [{"doc_id": str(d), "clause_id": c} for d, c in ids]}


def answer(fraud_type: str, fault: str, route: str, cited: list[str],
           position: str = "Drafted position [cited clauses].") -> dict:
    return {"action": "answer", "fraud_type": fraud_type, "fault_hypothesis": fault,
            "draft_route": route, "draft_position": position, "cited_clause_ids": cited}


# ---------- scripted Verifier replies (agent/prompts/verifier_v1.yaml format) ----------

def check(claims: list[tuple[str, bool, str]], searches: list[str]) -> dict:
    return {"claims": [{"claim": c, "supported": s, "clause_id": i} for c, s, i in claims],
            "searches": searches}


def judge(conflict: bool, clause_ids: list[str] = (), reason: str = "Reason.") -> dict:
    return {"conflict": conflict, "clause_ids": list(clause_ids), "reason": reason}
