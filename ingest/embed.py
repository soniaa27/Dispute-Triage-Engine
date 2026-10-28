"""Text embeddings for the index (ingest) and for queries (Retriever).

The default model runs locally through fastembed (ONNX, CPU): no API key, no rate limit,
and the same vectors at build and query time. Model files are cached in data/models/ so the
worker image can ship them. Tests use HashEmbedder and never download a model.
"""

import hashlib
import os
import re
from pathlib import Path
from typing import Protocol

import numpy as np

DEFAULT_EMBED_MODEL = "BAAI/bge-small-en-v1.5"
# Texts per ONNX call. fastembed's default (256) pads a batch to its longest text (up to 512
# tokens) and needs several GB for attention: the build hung on an 8 GB Mac and was killed
# in Docker (docs/incident_log.md, 2026-10-28). Small batches cost almost nothing here.
DEFAULT_EMBED_BATCH_SIZE = 8
MODEL_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "models"


def embed_model_name() -> str:
    """EMBED_MODEL from the environment (see .env.example), else the default."""
    return os.environ.get("EMBED_MODEL") or DEFAULT_EMBED_MODEL


def embed_batch_size() -> int:
    """EMBED_BATCH_SIZE from the environment (see .env.example), else the default."""
    value = os.environ.get("EMBED_BATCH_SIZE", "").strip()
    if not value:
        return DEFAULT_EMBED_BATCH_SIZE
    if not value.isdigit() or int(value) < 1:
        raise ValueError(f"EMBED_BATCH_SIZE must be a positive integer, got {value!r}")
    return int(value)


class Embedder(Protocol):
    model_name: str
    dim: int

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        """(len(texts), dim) float32, L2-normalised."""
        ...

    def embed_query(self, text: str) -> np.ndarray:
        """(dim,) float32, L2-normalised."""
        ...


def _normalise(vectors: np.ndarray) -> np.ndarray:
    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    return vectors / np.where(norms == 0, 1, norms)


class FastEmbedder:
    """fastembed TextEmbedding. passage_embed/query_embed apply the model's own prefixes
    (bge: "Represent this sentence for searching relevant passages: " on queries)."""

    def __init__(self, model_name: str | None = None, cache_dir: Path = MODEL_CACHE_DIR,
                 batch_size: int | None = None):
        from fastembed import TextEmbedding  # imported lazily: heavy, and unused in tests

        self.model_name = model_name or embed_model_name()
        self.batch_size = batch_size or embed_batch_size()
        self._model = TextEmbedding(self.model_name, cache_dir=str(cache_dir))
        self.dim = int(self.embed_query("dimension probe").shape[0])

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return _normalise(np.stack(list(
            self._model.passage_embed(texts, batch_size=self.batch_size))))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalise(next(iter(self._model.query_embed([text]))))


class HashEmbedder:
    """Deterministic bag-of-words vectors from hashed tokens. For tests only: no model
    download, but texts sharing words still score higher than unrelated texts."""

    model_name = "test-hash"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def _vector(self, text: str) -> np.ndarray:
        vector = np.zeros(self.dim, dtype=np.float32)
        for token in re.findall(r"[a-z0-9]+", text.lower()):
            vector[int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim] += 1
        return vector

    def embed_passages(self, texts: list[str]) -> np.ndarray:
        return _normalise(np.stack([self._vector(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return _normalise(self._vector(text))
