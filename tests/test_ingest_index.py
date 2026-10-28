import faulthandler
import time

import numpy as np
import pytest

from contracts.agent import DocId
from eval.corpus import corpus_available
from ingest.chunks import Chunk
from ingest.embed import DEFAULT_EMBED_BATCH_SIZE, HashEmbedder, embed_batch_size, embed_model_name
from ingest.index import build_index, embedding_text, load_index, search


def _chunk(clause_id: str, text: str, **kw) -> Chunk:
    return Chunk(doc_id=DocId.RBI_2017_CIRCULAR, clause_id=clause_id, text=text, page=3,
                 kind="clause", **kw)


CHUNKS = [
    _chunk("6", "A customer's entitlement to zero liability shall arise where:",
           section="(a) Zero Liability of a Customer"),
    _chunk("6(ii)", "Third party breach, customer notifies the bank within three working days.",
           section="(a) Zero Liability of a Customer", parent="6"),
    _chunk("12", "The burden of proving customer liability shall lie on the bank.",
           section="Burden of Proof"),
]


def test_embedding_text_adds_section_and_parent_lead_in():
    by_id = {c.citation_id: c for c in CHUNKS}
    text = embedding_text(CHUNKS[1], by_id)
    assert text.splitlines() == ["(a) Zero Liability of a Customer", CHUNKS[0].text,
                                 CHUNKS[1].text]
    assert embedding_text(CHUNKS[2], by_id) == "Burden of Proof\n" + CHUNKS[2].text


def test_build_load_and_search(tmp_path):
    embedder = HashEmbedder()
    meta = build_index(CHUNKS, embedder, tmp_path)
    assert meta == {"model": "test-hash", "dim": embedder.dim, "vectors": 3}
    index, ids = load_index(tmp_path)
    assert ids == [c.citation_id for c in CHUNKS]
    hits = search(index, embedder.embed_query("who bears the burden of proving liability"), k=2)
    assert ids[hits[0][0]] == "RBI_2017_CIRCULAR:12"
    assert len(hits) == 2 and hits[0][1] >= hits[1][1]
    assert len(search(index, embedder.embed_query("bank"), k=10)) == 3  # k > size


def test_hash_embedder_vectors_are_normalised():
    vectors = HashEmbedder().embed_passages(["a b c", "d"])
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0)
    assert np.allclose(np.linalg.norm(HashEmbedder().embed_query("")), 0.0)


def test_embed_model_from_environment(monkeypatch):
    monkeypatch.delenv("EMBED_MODEL", raising=False)
    assert embed_model_name() == "BAAI/bge-small-en-v1.5"
    monkeypatch.setenv("EMBED_MODEL", "other/model")
    assert embed_model_name() == "other/model"


def test_embed_batch_size_from_environment(monkeypatch):
    monkeypatch.delenv("EMBED_BATCH_SIZE", raising=False)
    assert embed_batch_size() == DEFAULT_EMBED_BATCH_SIZE == 8
    monkeypatch.setenv("EMBED_BATCH_SIZE", "16")
    assert embed_batch_size() == 16
    for bad in ("0", "-1", "eight"):
        monkeypatch.setenv("EMBED_BATCH_SIZE", bad)
        with pytest.raises(ValueError, match="EMBED_BATCH_SIZE"):
            embed_batch_size()


@pytest.mark.model
def test_real_model_ranks_relevant_passage_first():
    from ingest.embed import FastEmbedder

    embedder = FastEmbedder()
    passages = embedder.embed_passages([c.text for c in CHUNKS])
    query = embedder.embed_query("fraud reported within three working days, zero liability")
    assert int(np.argmax(passages @ query)) == 1


FULL_CORPUS_EMBED_LIMIT_S = 120


@pytest.mark.model
@pytest.mark.skipif(not corpus_available(), reason="corpus PDFs not present in corpus/raw/")
def test_real_model_embeds_the_full_corpus_in_bounded_time():
    """Embed every chunk of the real PDFs, exactly as `python -m ingest build` does.

    The other model test embeds 6 short texts, which fit in one small batch. That is why
    it passed while the real build hung: fastembed's default batch of 256 padded the whole
    corpus to 512 tokens and ran out of memory (docs/incident_log.md, 2026-10-28). This
    test uses the real corpus size and text lengths, so a batch size that blows up memory
    shows up here as a timeout or a crash. The bug made the build hang rather than slow
    down, so a watchdog aborts the test run (with a traceback) if embedding never returns.
    """
    from ingest.documents import chunk_corpus
    from ingest.embed import FastEmbedder
    from ingest.pdf import load_corpus

    chunks = chunk_corpus(load_corpus())
    by_id = {c.citation_id: c for c in chunks}
    texts = [embedding_text(c, by_id) for c in chunks]
    embedder = FastEmbedder()
    faulthandler.dump_traceback_later(2 * FULL_CORPUS_EMBED_LIMIT_S, exit=True)
    try:
        started = time.perf_counter()
        vectors = embedder.embed_passages(texts)
        elapsed = time.perf_counter() - started
    finally:
        faulthandler.cancel_dump_traceback_later()
    assert vectors.shape == (len(chunks), embedder.dim)
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-3)
    assert elapsed < FULL_CORPUS_EMBED_LIMIT_S, f"{len(chunks)} chunks took {elapsed:.0f}s"
