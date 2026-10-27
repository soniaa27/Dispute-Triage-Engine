"""Chunk: one clause (or one part of a long clause) of a corpus document.

Rules from docs/interfaces.md: every chunk has exactly one clause_id and never spans two;
a long clause is split into "5.12#1", "5.12#2"; a table is its own clause and each row is
also a chunk; linked definitions are recorded in `related`, never merged into the text.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from contracts.agent import CITATION_SEPARATOR, DocId, Passage

# Longer prose clauses are split at sentence ends. Tables are never split: get_clause must
# return the whole table, and every row is retrievable on its own anyway.
MAX_CHUNK_CHARS = 1500
PART_SEPARATOR = "#"

_SENTENCE_END = re.compile(r"(?<=[.;:])\s+")


class Chunk(BaseModel):
    model_config = ConfigDict(extra="forbid")

    doc_id: DocId
    clause_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    page: int = Field(ge=1, description="1-based PDF page index where the clause starts.")
    kind: Literal["clause", "table", "table_row", "note"]
    section: str | None = Field(default=None, description="Heading the clause sits under.")
    parent: str | None = Field(default=None, description="clause_id of the enclosing clause.")
    related: list[str] = Field(
        default_factory=list, description='Linked clauses, each "DOC_ID:clause_id".'
    )

    @property
    def citation_id(self) -> str:
        return f"{self.doc_id}{CITATION_SEPARATOR}{self.clause_id}"

    @property
    def base_clause_id(self) -> str:
        """clause_id without a "#n" part suffix."""
        return self.clause_id.split(PART_SEPARATOR, 1)[0]

    def to_passage(self, score: float | None = None) -> Passage:
        return Passage(doc_id=self.doc_id, clause_id=self.clause_id, text=self.text,
                       page=self.page, score=score)


def split_long(chunk: Chunk, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """Split a long prose clause at sentence ends into "<id>#1", "<id>#2", ..."""
    if chunk.kind in ("table", "table_row") or len(chunk.text) <= max_chars:
        return [chunk]
    parts: list[str] = []
    current = ""
    for sentence in _SENTENCE_END.split(chunk.text):
        if current and len(current) + 1 + len(sentence) > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}" if current else sentence
    parts.append(current)
    if len(parts) == 1:
        return [chunk]
    return [
        chunk.model_copy(update={"clause_id": f"{chunk.clause_id}{PART_SEPARATOR}{n}",
                                 "text": part})
        for n, part in enumerate(parts, start=1)
    ]
