import pytest

from contracts.agent import DocId
from contracts.tools import Retriever
from eval.corpus import corpus_available
from ingest.build import INDEX_DIR, MANIFEST_FILE, build
from ingest.chunks import Chunk
from ingest.embed import HashEmbedder
from ingest.index import build_index, load_index
from retrieval import HybridRetriever
from retrieval.bm25 import BM25, tokenize

RBI, IDBI = DocId.RBI_2017_CIRCULAR, DocId.IDBI_POLICY


def _chunk(doc_id, clause_id, text, **kw) -> Chunk:
    return Chunk(doc_id=doc_id, clause_id=clause_id, text=text, page=kw.pop("page", 3),
                 kind=kw.pop("kind", "clause"), **kw)


CHUNKS = [
    _chunk(RBI, "5#1", "Banks must ask customers to register for SMS alerts.", page=2),
    _chunk(RBI, "5#2", "Alerts shall be sent for every electronic transaction.", page=3),
    _chunk(RBI, "12", "The burden of proving customer liability shall lie on the bank."),
    _chunk(IDBI, "14.1", "The burden of proving customer liability shall lie on the Bank."),
    _chunk(IDBI, "Table-I.03", "Table - I, S. No 03\nLiability of Customer: Rs 10,000",
           kind="table_row", parent="Table-I", related=["IDBI_POLICY:5.13"]),
    _chunk(IDBI, "5.13", "Number of Days is calculated without the date of reporting."),
]


@pytest.fixture(scope="module")
def retriever(tmp_path_factory):
    embedder = HashEmbedder()
    index_dir = tmp_path_factory.mktemp("index")
    build_index(CHUNKS, embedder, index_dir)
    return HybridRetriever(CHUNKS, *load_index(index_dir), embedder)


def test_implements_the_contract(retriever):
    assert isinstance(retriever, Retriever)


def test_search_ranks_matching_clauses_first(retriever):
    passages = retriever.search_corpus("who carries the burden of proving liability", k=2)
    assert {p.citation_id for p in passages} == {"RBI_2017_CIRCULAR:12", "IDBI_POLICY:14.1"}
    assert passages[0].score >= passages[1].score > 0


def test_search_filters_by_document_and_respects_k(retriever):
    passages = retriever.search_corpus("burden of proving liability", k=3, doc_ids=[IDBI])
    assert len(passages) == 3 and {p.doc_id for p in passages} == {IDBI}
    assert passages[0].clause_id == "14.1"
    assert len(retriever.search_corpus("liability", k=100)) == len(CHUNKS)
    assert retriever.search_corpus("liability", k=0) == []
    assert retriever.search_corpus("   ") == []


def test_keyword_match_lifts_exact_terms(retriever):
    assert retriever.search_corpus("Rs 10,000", k=1)[0].clause_id == "Table-I.03"


def test_get_clause_exact_whole_and_unknown(retriever):
    row = retriever.get_clause(IDBI, "Table-I.03")
    assert row.text == CHUNKS[4].text and row.page == 3 and row.score is None
    whole = retriever.get_clause(RBI, "5")
    assert whole.clause_id == "5" and whole.page == 2
    assert whole.text == f"{CHUNKS[0].text} {CHUNKS[1].text}"
    assert retriever.get_clause(RBI, "5#2").text == CHUNKS[1].text
    assert retriever.get_clause(RBI, "99") is None
    assert retriever.get_clause(IDBI, "12") is None  # exists only in the other document
    assert retriever.get_clause("NOT_A_DOC", "12") is None


def test_related_links(retriever):
    assert retriever.related(IDBI, "Table-I.03") == ["IDBI_POLICY:5.13"]
    assert retriever.related(RBI, "5") == [] and retriever.related(RBI, "99") == []


def test_mismatched_index_is_rejected(tmp_path):
    build_index(CHUNKS, HashEmbedder(), tmp_path)
    with pytest.raises(ValueError, match="rebuild"):
        HybridRetriever(CHUNKS[::-1], *load_index(tmp_path), HashEmbedder())


def test_bm25_scores_rare_terms_higher():
    bm25 = BM25(["card lost", "card stolen", "card card card"])
    assert tokenize("Add-on Card, Rs 5,000") == ["add", "on", "card", "rs", "5", "000"]
    scores = bm25.scores("stolen card")
    assert scores[1] == max(scores) and scores[0] > 0
    assert bm25.scores("unrelated") == [0.0, 0.0, 0.0]


corpus = pytest.mark.skipif(not corpus_available(), reason="corpus PDFs not present")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    build(out_dir=out, embedder=HashEmbedder())
    return out


@corpus
def test_load_built_corpus(built):
    retriever = HybridRetriever.load(built, embedder=HashEmbedder())
    whole = retriever.get_clause(RBI, "5")
    assert whole.text.startswith("Banks must ask their customers to mandatorily register")
    table = retriever.get_clause(IDBI, "Table-I")
    assert table.text.startswith("Table - I") and "S. No" in table.text
    top = retriever.search_corpus("burden of proving customer liability", k=2)
    assert {p.clause_id for p in top} == {"12", "14.1"}


@corpus
def test_load_refuses_a_different_query_model(built, tmp_path):
    class Other(HashEmbedder):
        model_name = "other-model"

    with pytest.raises(ValueError, match="index built with test-hash"):
        HybridRetriever.load(built, embedder=Other())
    with pytest.raises(FileNotFoundError):
        HybridRetriever.load(tmp_path)


@pytest.mark.model
@corpus
def test_real_index_finds_scenario_clauses():
    """Against data/ built by `python -m ingest build` with the real model."""
    from ingest.build import DATA_DIR

    if not (DATA_DIR / MANIFEST_FILE).is_file() or not (DATA_DIR / INDEX_DIR).is_dir():
        pytest.skip("run python -m ingest build first")
    retriever = HybridRetriever.load()
    ids = [p.citation_id for p in retriever.search_corpus(
        "customer shared the OTP with a caller; is the customer liable?", k=5)]
    assert "IDBI_POLICY:5.12" in ids
    ids = [p.citation_id for p in retriever.search_corpus(
        "bank must credit the amount within 10 working days", k=5)]
    assert {"RBI_2017_CIRCULAR:9", "IDBI_POLICY:7.1"} & set(ids)  # same rule in both


def test_single_ranking_modes(retriever):
    keyword = retriever.search("Rs 10,000", k=5, mode="keyword")
    assert [p.clause_id for p in keyword] == ["Table-I.03"]  # only chunk with those words
    assert keyword[0].score > 0
    dense = retriever.search("burden of proving liability", k=2, mode="dense")
    assert {p.clause_id for p in dense} == {"12", "14.1"} and dense[0].score <= 1.0
    assert retriever.search_corpus("liability", 3) == retriever.search("liability", 3)
