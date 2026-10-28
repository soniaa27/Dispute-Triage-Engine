"""HybridRetriever: the contracts.tools.Retriever over data/chunks.jsonl + data/index/.

search_corpus ranks every chunk twice, by meaning (FAISS, cosine over the embeddings) and
by keywords (BM25), and merges the two rankings with Reciprocal Rank Fusion:
score = sum over rankings of 1 / (RRF_K + rank). RRF needs no score calibration between
the two methods, and a chunk ranked high by either one comes out near the top.

Both rankings see the same text the index was built from (section heading, parent
lead-in, clause text); passages carry only the clause text.

Tool-call counting and limits are the graph's job (AGENT_MAX_TOOL_CALLS), not this class.
"""

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import faiss

from contracts.agent import CITATION_SEPARATOR, DocId, Passage
from ingest.build import DATA_DIR, INDEX_DIR, load_chunks, read_manifest
from ingest.chunks import Chunk
from ingest.embed import Embedder, FastEmbedder
from ingest.index import embedding_text, load_index, search
from retrieval.bm25 import BM25

RRF_K = 60
# How many of each ranking take part in the fusion.
FUSION_DEPTH = 50

Mode = Literal["hybrid", "dense", "keyword"]
MODES: tuple[Mode, ...] = ("hybrid", "dense", "keyword")


def _key(doc_id: DocId | str, clause_id: str) -> str:
    return f"{DocId(doc_id)}{CITATION_SEPARATOR}{clause_id}"


class HybridRetriever:
    def __init__(self, chunks: list[Chunk], index: faiss.Index, ids: list[str],
                 embedder: Embedder):
        if ids != [c.citation_id for c in chunks]:
            raise ValueError("index ids do not match the chunks; rebuild: python -m ingest build")
        self._chunks = chunks
        self._index = index
        self._embedder = embedder
        by_id = {c.citation_id: c for c in chunks}
        self._bm25 = BM25([embedding_text(c, by_id) for c in chunks])
        self._by_id = by_id
        self._parts: dict[str, list[Chunk]] = defaultdict(list)
        for chunk in chunks:
            if chunk.clause_id != chunk.base_clause_id:
                self._parts[_key(chunk.doc_id, chunk.base_clause_id)].append(chunk)

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR, embedder: Embedder | None = None
             ) -> "HybridRetriever":
        """Load a built corpus. Queries are embedded with the model the index was built
        with (from the manifest), so the two always match."""
        manifest = read_manifest(data_dir)
        if manifest is None:
            raise FileNotFoundError(f"{data_dir}: no manifest; run: python -m ingest build")
        model = manifest["index"]["model"]
        if embedder is None:
            embedder = FastEmbedder(model)
        elif embedder.model_name != model:
            raise ValueError(f"index built with {model}, queries would use {embedder.model_name}")
        index, ids = load_index(data_dir / INDEX_DIR)
        return cls(load_chunks(data_dir), index, ids, embedder)

    def search_corpus(self, query: str, k: int = 5,
                      doc_ids: Sequence[DocId] | None = None) -> list[Passage]:
        return self.search(query, k, doc_ids)

    def search(self, query: str, k: int = 5, doc_ids: Sequence[DocId] | None = None,
               mode: Mode = "hybrid") -> list[Passage]:
        """search_corpus with a choice of ranking; "dense" and "keyword" alone exist for
        the retrieval metrics (python -m retrieval.evaluate), which compare all three."""
        if k <= 0 or not query.strip():
            return []
        allowed = {DocId(d) for d in doc_ids} if doc_ids is not None else set(DocId)
        rankings = []
        if mode in ("hybrid", "dense"):
            dense = search(self._index, self._embedder.embed_query(query), self._index.ntotal)
            rankings.append([(row, s) for row, s in dense
                             if self._chunks[row].doc_id in allowed])
        if mode in ("hybrid", "keyword"):
            keyword = self._bm25.scores(query)
            rankings.append(sorted(((row, s) for row, s in enumerate(keyword)
                                    if s > 0 and self._chunks[row].doc_id in allowed),
                                   key=lambda hit: (-hit[1], hit[0])))
        if mode == "hybrid":
            fused: dict[int, float] = defaultdict(float)
            for ranking in rankings:
                for rank, (row, _) in enumerate(ranking[:FUSION_DEPTH], start=1):
                    fused[row] += 1 / (RRF_K + rank)
            hits = sorted(fused.items(), key=lambda hit: (-hit[1], hit[0]))
        else:
            hits = rankings[0]
        return [self._chunks[row].to_passage(score=round(score, 6)) for row, score in hits[:k]]

    def get_clause(self, doc_id: DocId, clause_id: str) -> Passage | None:
        """Exact lookup. A clause stored in parts ("5#1", "5#2") is returned whole under its
        own id; a single part can still be asked for by its "#n" id."""
        try:
            key = _key(doc_id, clause_id)
        except ValueError:
            return None
        if chunk := self._by_id.get(key):
            return chunk.to_passage()
        if parts := self._parts.get(key):
            return Passage(doc_id=parts[0].doc_id, clause_id=clause_id, page=parts[0].page,
                           text=" ".join(p.text for p in parts))
        return None

    def related(self, doc_id: DocId, clause_id: str) -> list[str]:
        """Linked clauses ("DOC_ID:clause_id") recorded at ingest, e.g. a table row's
        definitions. [] for unknown ids."""
        try:
            key = _key(doc_id, clause_id)
        except ValueError:
            return []
        chunk = self._by_id.get(key) or next(iter(self._parts.get(key, [])), None)
        return list(chunk.related) if chunk else []
