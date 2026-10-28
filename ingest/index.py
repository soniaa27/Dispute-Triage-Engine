"""Vector index over the chunks: data/index/{vectors.faiss, ids.json}.

Each chunk is embedded with its context (section heading and the start of its parent
clause, e.g. 6(ii) with paragraph 6's "zero liability shall arise where ..."), so a
sub-clause is found by what it is about, not only by its own words. The cited text is
unchanged; context is used for the vector only.

Exact inner-product search over L2-normalised vectors (cosine). A few hundred chunks need
no approximate index.
"""

import json
from pathlib import Path

import faiss
import numpy as np

from ingest.chunks import Chunk
from ingest.embed import Embedder

VECTORS_FILE = "vectors.faiss"
IDS_FILE = "ids.json"
PARENT_CONTEXT_CHARS = 300


def embedding_text(chunk: Chunk, by_id: dict[str, Chunk]) -> str:
    parts = [chunk.section] if chunk.section else []
    parent = by_id.get(f"{chunk.doc_id}:{chunk.parent}") if chunk.parent else None
    if parent and parent.kind != "table":
        parts.append(parent.text[:PARENT_CONTEXT_CHARS])
    parts.append(chunk.text)
    return "\n".join(parts)


def build_index(chunks: list[Chunk], embedder: Embedder, index_dir: Path) -> dict:
    """Embed every chunk and write the index. Returns the manifest's "index" section."""
    by_id = {c.citation_id: c for c in chunks}
    vectors = embedder.embed_passages([embedding_text(c, by_id) for c in chunks])
    index = faiss.IndexFlatIP(embedder.dim)
    index.add(vectors)

    index_dir.mkdir(parents=True, exist_ok=True)
    tmp = index_dir / (VECTORS_FILE + ".tmp")
    faiss.write_index(index, str(tmp))
    tmp.replace(index_dir / VECTORS_FILE)
    tmp = index_dir / (IDS_FILE + ".tmp")
    tmp.write_text(json.dumps([c.citation_id for c in chunks]), encoding="utf-8")
    tmp.replace(index_dir / IDS_FILE)
    return {"model": embedder.model_name, "dim": embedder.dim, "vectors": index.ntotal}


def load_index(index_dir: Path) -> tuple[faiss.Index, list[str]]:
    index = faiss.read_index(str(index_dir / VECTORS_FILE))
    ids = json.loads((index_dir / IDS_FILE).read_text(encoding="utf-8"))
    if index.ntotal != len(ids):
        raise ValueError(f"{index_dir}: {index.ntotal} vectors but {len(ids)} ids")
    return index, ids


def search(index: faiss.Index, query: np.ndarray, k: int) -> list[tuple[int, float]]:
    """(row, cosine score) for the k nearest rows, best first."""
    scores, rows = index.search(query.reshape(1, -1).astype(np.float32), min(k, index.ntotal))
    return [(int(r), float(s)) for r, s in zip(rows[0], scores[0], strict=True) if r >= 0]
