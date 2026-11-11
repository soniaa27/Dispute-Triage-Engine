"""Evaluation preparation (L2, L3; docs/evaluation.md): the Verifier prompt variant, the
opt-in retrieval down-ranking, and the eval runner running the real graph and recording
what it ran. No model is called: the graph runs on the gateway's stub provider."""

import json

import pytest

from agent.graph import GraphSettings, run_graph
from contracts import RunStatus
from eval import run_eval as run_eval_module
from eval.run_eval import main, run_eval
from eval.schema import DEFAULT_SCENARIOS, load_scenarios
from retrieval import hybrid
from retrieval.hybrid import DOWNRANK_FACTOR, HybridRetriever
from tests.agent_doubles import ScriptedLLM
from tests.test_graph import DRAFT, LOOKUP, _request
from tests.test_stub_agents import StubLLM
from tests.test_verifier import CLAUSES, LINKS, SUPPORTED

# ---------- L2: Verifier prompt variant ----------


def test_verifier_v2_is_selected_by_setting_and_recorded(monkeypatch):
    from tests.agent_doubles import DictRetriever

    llm = ScriptedLLM([LOOKUP, DRAFT, SUPPORTED, {"conflict": False, "clause_ids": [],
                                                  "reason": "None."}])
    retriever = DictRetriever(CLAUSES, LINKS, {"customer negligence": ["IDBI_POLICY:Table-II.03"]})
    result = run_graph(_request(), "run-v2", retriever, llm,
                       settings=GraphSettings(verifier_prompt_version="v2"))
    assert result.prompt_version == "inv-v1+ver-v2"
    check_prompt = [p for step, p in llm.calls if step == "verifier"][0]
    assert "Check them one at a time and strictly" in check_prompt
    assert [t["prompt_version"] for t in result.trace if t.get("agent") == "verifier"] == [
        "verifier_v2", "verifier_v2"]

    monkeypatch.setenv("AGENT_VERIFIER_PROMPT_VERSION", "v2")
    assert GraphSettings().verifier_prompt_version == "v2"
    monkeypatch.setenv("AGENT_VERIFIER_PROMPT_VERSION", "2")
    with pytest.raises(ValueError):
        GraphSettings()


def test_verifier_v1_and_v2_differ_only_in_check_rule_3():
    from agent.prompts import load_prompts

    v1 = load_prompts("verifier_v1")["agents"]["verifier"]
    v2 = load_prompts("verifier_v2")["agents"]["verifier"]
    assert v1["judge"] == v2["judge"]
    before, after = v1["check"].split("\n"), v2["check"].split("\n")
    rule3 = lambda lines: (lines.index(next(x for x in lines if x.lstrip().startswith("3."))),  # noqa: E731
                           lines.index(next(x for x in lines if x.lstrip().startswith("4."))))
    (a1, b1), (a2, b2) = rule3(before), rule3(after)
    assert before[:a1] == after[:a2] and before[b1:] == after[b2:]
    assert before[a1:b1] != after[a2:b2]


# ---------- L3: retrieval down-ranking (opt-in) ----------


def _scores(retriever, query):
    return {p.citation_id: p.score for p in retriever.search_corpus(query, k=400)}


def test_downranking_is_off_by_default(real_corpus, monkeypatch):
    monkeypatch.delenv(hybrid.DOWNRANK_ENV, raising=False)
    assert real_corpus[1].downrank_tables is False


def test_downranking_halves_annex_rows_and_whole_tables_only(real_corpus):
    from ingest.embed import HashEmbedder

    data_dir, off = real_corpus
    on = HybridRetriever.load(data_dir, embedder=HashEmbedder(), downrank_tables=True)
    query = "customer liability table reporting delay working days annex"
    before, after = _scores(off, query), _scores(on, query)
    assert before.keys() == after.keys()
    changed = {cid for cid in before if abs(after[cid] - before[cid]) > 1e-9}
    assert changed, "the query should reach some tables or Annex rows"
    for cid in changed:
        assert after[cid] == pytest.approx(before[cid] * DOWNRANK_FACTOR, abs=2e-6)
        doc, clause = cid.split(":", 1)
        assert clause.startswith("Annex-") or clause in ("Table-1", "Table-2", "Table-I",
                                                         "Table-II"), cid
    assert not any(c.startswith("RBI_IOS_2026:Annex") for c in changed)


def test_downranking_reads_the_environment(real_corpus, monkeypatch):
    from ingest.embed import HashEmbedder

    monkeypatch.setenv(hybrid.DOWNRANK_ENV, "true")
    assert HybridRetriever.load(real_corpus[0], embedder=HashEmbedder()).downrank_tables


# ---------- eval runner: the real graph, recorded ----------


@pytest.fixture
def graph_on_stub(monkeypatch, real_corpus):
    """runner.run_agent(mode="graph") with the real retriever and the stub provider."""
    from agent import runner

    monkeypatch.setattr(runner, "get_retriever", lambda: real_corpus[1])
    monkeypatch.setattr(runner, "get_llm_client", lambda: StubLLM())
    monkeypatch.setattr(run_eval_module, "gateway_provider", lambda: "stub")


def test_run_eval_runs_the_graph_and_records_provenance(graph_on_stub, tmp_path):
    from eval.chunks import load_chunk_index

    chosen = [s for s in load_scenarios(DEFAULT_SCENARIOS) if s.id in ("S01", "S14")]
    report, json_path, md_path = run_eval(
        chosen, prompt_version="graph-stub", agent_mode="graph",
        scenarios_file=DEFAULT_SCENARIOS, out_dir=tmp_path, chunks=load_chunk_index())
    meta = json.loads(json_path.read_text())["meta"]
    assert meta["agent_mode"] == "graph" and meta["provider"] == "stub"
    assert meta["agent_prompt_versions"] == ["inv-v1+ver-v1"]
    assert meta["agent_models"] == ["stub"]
    assert meta["verifier_prompt_version"] == "v1"
    assert meta["retrieval_downrank_tables"] is False
    statuses = {s["id"]: s["result"]["status"] for s in report["scenarios"]}
    assert statuses == {"S01": RunStatus.completed, "S14": RunStatus.escalated}
    assert report["metrics"]["route_accuracy"] == 1.0
    assert "Agent mode: graph; provider: stub" in md_path.read_text()


def test_live_provider_needs_confirmation(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(run_eval_module, "gateway_provider", lambda: "gemini")
    called = []
    monkeypatch.setattr(run_eval_module, "run_eval", lambda *a, **k: called.append(1))
    with pytest.raises(SystemExit) as exc:
        main(["--agent-mode", "graph", "--ids", "S01", "--out", str(tmp_path)])
    assert exc.value.code == 2 and not called
    assert "--confirm-live" in capsys.readouterr().err
