import numpy as np
import pytest

from contracts.agent import DocId
from ingest.chunks import Chunk
from ingest.embed import HashEmbedder, embed_model_name
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


@pytest.mark.model
def test_real_model_ranks_relevant_passage_first():
    from ingest.embed import FastEmbedder

    embedder = FastEmbedder()
    passages = embedder.embed_passages([c.text for c in CHUNKS])
    query = embedder.embed_query("fraud reported within three working days, zero liability")
    assert int(np.argmax(passages @ query)) == 1
