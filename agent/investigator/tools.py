"""The Investigator's tools: corpus search and clause lookup, run through the Retriever.

The model asks for tool calls (SearchRequest, LookupRequest); this module runs them and
keeps every passage returned, so the draft can only cite text that was actually retrieved.

The retriever passed in is the graph's wrapper: it counts tool calls against the shared
limit, logs each call to the trace and raises ToolBudgetExceeded at the limit. Nothing here
catches that, or counts or logs calls itself.
"""

from pydantic import BaseModel, ConfigDict, Field

from contracts.agent import DocId, Passage
from contracts.tools import Retriever

SEARCH_K = 5
# Per model turn, so one turn cannot spend the run's shared tool-call budget.
MAX_SEARCHES_PER_TURN = 4
MAX_LOOKUPS_PER_TURN = 4


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=3, max_length=300)
    doc_ids: list[DocId] | None = None


class LookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: DocId
    clause_id: str = Field(min_length=1, max_length=40)


class Evidence:
    """Passages retrieved so far, in first-seen order, and the searches run."""

    def __init__(self) -> None:
        self._passages: dict[str, Passage] = {}
        self.queries_run: list[str] = []

    @property
    def passages(self) -> list[Passage]:
        return list(self._passages.values())

    def add(self, passages: list[Passage]) -> list[Passage]:
        """Keep passages not seen before; return them."""
        new = [p for p in passages if p.citation_id not in self._passages]
        for passage in new:
            self._passages[passage.citation_id] = passage
        return new

    def __contains__(self, citation_id: str) -> bool:
        return citation_id in self._passages


def run_tools(retriever: Retriever, evidence: Evidence, searches: list[SearchRequest],
              lookups: list[LookupRequest]) -> list[str]:
    """Run one turn's tool calls; record results in `evidence`. Returns one observation
    line per call, for the next prompt. Requests over the per-turn caps are not run."""
    observations = []
    for search in searches[:MAX_SEARCHES_PER_TURN]:
        found = retriever.search_corpus(search.query, k=SEARCH_K, doc_ids=search.doc_ids)
        evidence.queries_run.append(search.query)
        evidence.add(found)
        ids = ", ".join(p.citation_id for p in found) or "nothing"
        observations.append(f'search "{search.query}" -> {ids}')
    for lookup in lookups[:MAX_LOOKUPS_PER_TURN]:
        passage = retriever.get_clause(lookup.doc_id, lookup.clause_id)
        citation_id = f"{lookup.doc_id}:{lookup.clause_id}"
        if passage is None:
            observations.append(f"get_clause {citation_id} -> not found")
        else:
            evidence.add([passage])
            observations.append(f"get_clause {citation_id} -> found")
    skipped = (max(len(searches) - MAX_SEARCHES_PER_TURN, 0)
               + max(len(lookups) - MAX_LOOKUPS_PER_TURN, 0))
    if skipped:
        observations.append(f"{skipped} request(s) over the per-turn limit were not run")
    return observations
