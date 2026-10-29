"""Stage a: the citation check, in code (no LLM).

Every clause the draft cites must exist (retriever.get_clause on its base id) and the text
the result will cite must be a verbatim span of that clause, at most 60 words. These are the
rules of eval/chunks.py ChunkIndex.check_citation (docs/interfaces.md, "Citation rules"),
implemented here so the agent does not depend on eval/; tests/test_verifier.py checks the
two agree on every real chunk.

- Split clauses are cited under the base id: "RBI_2017_CIRCULAR:5#2" -> "RBI_2017_CIRCULAR:5".
  get_clause on the base id returns all parts joined, as eval does.
- Matching is whitespace-blind after NFKC and ASCII quotes and dashes, as eval.corpus.normalise.
"""

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum

from contracts.agent import DocId, InvestigatorOutput, Passage
from contracts.result import Citation
from contracts.tools import Retriever

MAX_CITATION_WORDS = 60
PART_SEPARATOR = "#"  # ingest.chunks: "5#2" is part 2 of clause "5"

_PUNCT = str.maketrans({"‘": "'", "’": "'", "‟": '"', "“": '"', "”": '"', "–": "-", "—": "-"})
_WHITESPACE = re.compile(r"\s+")


class CitationFailure(StrEnum):
    unknown_clause = "unknown_clause"  # get_clause found no clause with this id
    too_long = "too_long"  # more than MAX_CITATION_WORDS words
    text_not_in_chunk = "text_not_in_chunk"  # not a verbatim span of the clause's text


def normalise(text: str) -> str:
    return _WHITESPACE.sub("", unicodedata.normalize("NFKC", text).translate(_PUNCT))


def base_clause_id(clause_id: str) -> str:
    return clause_id.split(PART_SEPARATOR, 1)[0]


def citation_span(text: str, max_words: int = MAX_CITATION_WORDS) -> str:
    """The first max_words words of a passage: verbatim under the whitespace-blind
    comparison and within the word limit."""
    return " ".join(text.split()[:max_words])


def to_citation(passage: Passage) -> Citation:
    """A Citation under the base clause id, quoting the start of the passage."""
    return Citation(doc_id=str(passage.doc_id), clause_id=base_clause_id(passage.clause_id),
                    text=citation_span(passage.text))


def check_text(clause: Passage | None, text: str) -> CitationFailure | None:
    """None if `text` may be cited from `clause` (the get_clause result), else the first
    rule it breaks, in eval's order."""
    if clause is None:
        return CitationFailure.unknown_clause
    if len(text.split()) > MAX_CITATION_WORDS:
        return CitationFailure.too_long
    needle = normalise(text)
    if not needle or needle not in normalise(clause.text):
        return CitationFailure.text_not_in_chunk
    return None


def check_citation(retriever: Retriever, citation: Citation) -> tuple[Passage | None,
                                                                     CitationFailure | None]:
    """Fetch the cited clause by its base id and check the quoted text against it."""
    try:
        doc_id = DocId(citation.doc_id)
    except ValueError:
        return None, CitationFailure.unknown_clause
    clause = retriever.get_clause(doc_id, base_clause_id(citation.clause_id))
    return clause, check_text(clause, citation.text)


@dataclass(frozen=True)
class CheckedCitation:
    citation: Citation  # what the result would cite (base id, verbatim span)
    clause: Passage | None  # the clause as get_clause returned it
    failure: CitationFailure | None

    @property
    def citation_id(self) -> str:
        return f"{self.citation.doc_id}:{self.citation.clause_id}"

    @property
    def ok(self) -> bool:
        return self.failure is None


def check_draft_citations(draft: InvestigatorOutput, retriever: Retriever
                          ) -> list[CheckedCitation]:
    """One check (one get_clause call) per cited clause; parts of one clause are checked
    once, under the base id."""
    passages = {p.citation_id: p for p in draft.passages}
    checked, seen = [], set()
    for citation_id in draft.cited_clause_ids:
        citation = to_citation(passages[citation_id])
        key = (citation.doc_id, citation.clause_id)
        if key in seen:
            continue
        seen.add(key)
        clause, failure = check_citation(retriever, citation)
        checked.append(CheckedCitation(citation, clause, failure))
    return checked
