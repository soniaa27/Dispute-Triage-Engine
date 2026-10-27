import pytest

from contracts.agent import DocId, Passage
from eval.corpus import corpus_available
from eval.schema import load_scenarios
from ingest.build import build
from ingest.embed import HashEmbedder
from retrieval import HybridRetriever
from retrieval.evaluate import evaluate, main, markdown_summary, ranks, score_mode

S01 = next(s for s in load_scenarios() if s.id == "S01")
# S01 supporting: RBI 6(ii) (primary), IDBI Table-I.01, IDBI 5.20; must_not_cite RBI 7(i).


def _p(citation_id: str) -> Passage:
    doc, clause = citation_id.split(":", 1)
    return Passage(doc_id=DocId(doc), clause_id=clause, text="x")


class Fixed:
    """Returns the same ranking for every query; records the queries it saw."""

    def __init__(self, *citation_ids: str):
        self.passages = [_p(c) for c in citation_ids]
        self.queries: list[str] = []

    def search(self, query, k=5, doc_ids=None, mode="hybrid"):
        self.queries.append(query)
        return self.passages[:k]


def test_metrics_arithmetic():
    retriever = Fixed("IDBI_POLICY:Table-I.01", "RBI_2017_CIRCULAR:7(i)",
                      "RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:9.1")
    result = score_mode(retriever, [S01], "hybrid", (1, 3, 5), lambda s: ["q"])
    m = result["metrics"]
    assert (m["recall@1"], m["recall@3"], m["recall@5"]) == (round(1 / 3, 3), 0.667, 0.667)
    assert (m["hit@1"], m["primary_recall@1"], m["primary_recall@3"]) == (1.0, 0.0, 1.0)
    assert m["mrr"] == 1.0 and (m["must_not_cite@1"], m["must_not_cite@3"]) == (0, 1)
    assert result["scenarios"][0]["supporting"] == {
        "RBI_2017_CIRCULAR:6(ii)": 3, "IDBI_POLICY:Table-I.01": 1, "IDBI_POLICY:5.20": None}


def test_nothing_found_scores_zero():
    m = score_mode(Fixed("IDBI_POLICY:9.1"), [S01], "dense", (5,), lambda s: ["q"])["metrics"]
    assert (m["recall@5"], m["hit@5"], m["mrr"]) == (0.0, 0.0, 0.0)


def test_split_parts_count_as_their_clause_and_queries_are_fused():
    retriever = Fixed("RBI_2017_CIRCULAR:5#1", "RBI_2017_CIRCULAR:5#2", "IDBI_POLICY:9.1")
    retrieved, _ = ranks(retriever, ["first", "second"], "hybrid")
    assert retrieved == ["RBI_2017_CIRCULAR:5", "IDBI_POLICY:9.1"]
    assert retriever.queries == ["first", "second"]


def test_report_shape_and_summary():
    report = evaluate(Fixed("RBI_2017_CIRCULAR:6(ii)"), [S01], modes=("hybrid", "keyword"),
                      ks=(1, 5), model="m")
    assert report["meta"]["supporting_clause_count"] == 3
    assert report["meta"]["query_source"] == "complaint"
    assert set(report["modes"]) == {"hybrid", "keyword"}
    summary = markdown_summary(report)
    assert "| hybrid | 0.333 |" in summary
    assert "- S01: IDBI_POLICY:Table-I.01, IDBI_POLICY:5.20" in summary


@pytest.mark.skipif(not corpus_available(), reason="corpus PDFs not present")
def test_cli_on_a_built_corpus(tmp_path, capsys, monkeypatch):
    data = tmp_path / "data"
    build(out_dir=data, embedder=HashEmbedder())
    load = HybridRetriever.load.__func__  # the real model is never loaded in tests
    monkeypatch.setattr(HybridRetriever, "load", classmethod(
        lambda cls, d, embedder=None: load(cls, d, HashEmbedder())))
    assert main(["--data", str(data), "--out", str(tmp_path / "reports"), "--k", "5"]) == 0
    out = capsys.readouterr().out
    assert "| hybrid |" in out and "| keyword |" in out
    assert len(list((tmp_path / "reports").glob("*_retrieval-complaint.json"))) == 1

