"""Read-only lookup of the chunker's output (data/chunks.jsonl, written by `make ingest`).

Citations are checked against chunk text, the text a retriever returns (requirement A3),
not against the raw PDF: the chunker legitimately reformats some text (table rows labelled
by column, running page headers dropped from clauses that cross a page break, bullet
glyphs removed). See "Citation rules" in docs/interfaces.md.

Matching uses eval.corpus.normalise (whitespace ignored, NFKC, ASCII quotes and dashes), the
same "verbatim" as the PDF checks.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from contracts.agent import CITATION_SEPARATOR
from eval.corpus import base_citation_id, normalise
from ingest.build import CHUNKS_FILE, DATA_DIR, load_chunks
from ingest.chunks import Chunk

MAX_CITATION_WORDS = 60
_NUMBER = re.compile(r"\d[\d,]*\d|\d")


class ChunksMissingError(FileNotFoundError):
    def __init__(self, data_dir: Path):
        super().__init__(f"chunk output not found at {data_dir / CHUNKS_FILE}; "
                         "run `make ingest` (python -m ingest build) first")


class CitationFailure(StrEnum):
    unknown_clause = "unknown_clause"  # no chunk has this clause_id
    too_long = "too_long"  # more than MAX_CITATION_WORDS words
    text_not_in_chunk = "text_not_in_chunk"  # not a verbatim span of the chunk's text


@dataclass(frozen=True)
class ClauseText:
    kind: str
    text: str  # all "#n" parts of the clause joined in order


class ChunkIndex:
    """Chunk text by citation id. "#n" parts of a split clause are looked up as one clause:
    "RBI_2017_CIRCULAR:5" and "RBI_2017_CIRCULAR:5#2" both name the whole of paragraph 5."""

    def __init__(self, chunks: Iterable[Chunk]):
        parts: dict[str, list[Chunk]] = {}
        for chunk in chunks:
            parts.setdefault(base_citation_id(chunk.citation_id), []).append(chunk)
        self._clauses = {
            cid: ClauseText(group[0].kind, " ".join(c.text for c in group))
            for cid, group in parts.items()
        }
        self._normalised = {cid: normalise(c.text) for cid, c in self._clauses.items()}

    def __len__(self) -> int:
        return len(self._clauses)

    def __contains__(self, citation_id: str) -> bool:
        return base_citation_id(citation_id) in self._clauses

    def get(self, citation_id: str) -> ClauseText | None:
        return self._clauses.get(base_citation_id(citation_id))

    def contains_span(self, citation_id: str, text: str) -> bool:
        needle = normalise(text)
        haystack = self._normalised.get(base_citation_id(citation_id))
        return bool(needle) and haystack is not None and needle in haystack

    def check_citation(self, doc_id: str, clause_id: str, text: str) -> CitationFailure | None:
        """None if the citation resolves, else the first rule it breaks."""
        citation_id = f"{doc_id}{CITATION_SEPARATOR}{clause_id}"
        if citation_id not in self:
            return CitationFailure.unknown_clause
        if len(text.split()) > MAX_CITATION_WORDS:
            return CitationFailure.too_long
        if not self.contains_span(citation_id, text):
            return CitationFailure.text_not_in_chunk
        return None

    def missing_key_values(self, citation_id: str, text: str) -> list[str]:
        """Numbers in `text` (amounts, day counts, row numbers) absent from the clause, ignoring
        commas. For table rows, whose chunks do not keep the PDF's reading order."""
        clause = self.get(citation_id)
        have = {n.replace(",", "") for n in _NUMBER.findall(clause.text)} if clause else set()
        return [n for n in _NUMBER.findall(text) if n.replace(",", "") not in have]


def chunks_available(data_dir: Path = DATA_DIR) -> bool:
    return (data_dir / CHUNKS_FILE).is_file()


def load_chunk_index(data_dir: Path = DATA_DIR) -> ChunkIndex:
    """ChunkIndex over data/chunks.jsonl. Raises ChunksMissingError if it was never built."""
    if not chunks_available(data_dir):
        raise ChunksMissingError(data_dir)
    return ChunkIndex(load_chunks(data_dir))
