import json
from copy import deepcopy

import pytest

from agent import runner
from contracts import Citation, FraudType, Route, RunResult, RunStatus
from eval import compare
from eval.corpus import corpus_available, load_corpus_text
from eval.metrics import (
    NOT_COMPUTED_NO_CLIENT,
    ScenarioRun,
    basis_mention_rate,
    basis_missing_terms,
    deterministic_metrics,
    judged_metrics,
    label_counts,
    parse_json_reply,
)
from eval.run_eval import run_eval
from eval.schema import DEFAULT_SCENARIOS, Scenario, load_raw, load_scenarios
from eval.validate import validate

needs_corpus = pytest.mark.skipif(
    not corpus_available(), reason="corpus PDFs not present in corpus/raw/"
)


@pytest.fixture(scope="module")
def corpus():
    return load_corpus_text()


@pytest.fixture(scope="module")
def raw_scenarios():
    return load_raw(DEFAULT_SCENARIOS)


@pytest.fixture(scope="module")
def scenarios():
    return {s.id: s for s in load_scenarios(DEFAULT_SCENARIOS)}


# --- validation -------------------------------------------------------------------------


@needs_corpus
def test_scenarios_file_passes_validation(raw_scenarios, corpus):
    report = validate(raw_scenarios, corpus)
    assert report.ok, (report.errors, report.coverage_errors)
    assert report.total >= 24


@needs_corpus
def test_wrong_working_days_fails_validation(raw_scenarios, corpus):
    broken = deepcopy(raw_scenarios)
    broken[0]["expected"]["working_days_delay"] += 1
    report = validate(broken, corpus)
    assert not report.ok
    assert any("working_days_delay" in e for e in report.errors[broken[0]["id"]])


@needs_corpus
def test_altered_passage_fails_validation(raw_scenarios, corpus):
    broken = deepcopy(raw_scenarios)
    target = next(s for s in broken if "2,500" in s["expected"]["supporting_passage"])
    target["expected"]["supporting_passage"] = target["expected"]["supporting_passage"].replace(
        "2,500", "5,000"
    )
    report = validate(broken, corpus)
    assert any("not found verbatim" in e for e in report.errors[target["id"]])


@needs_corpus
def test_bad_clause_id_and_coverage_fail(raw_scenarios, corpus):
    broken = deepcopy(raw_scenarios[:5])
    broken[0]["expected"]["supporting_clauses"][0] = "IDBI_POLICY:Table 1 row 2"
    report = validate(broken, corpus)
    assert any("conventions" in e for e in report.errors[broken[0]["id"]])
    assert any("need 20" in e for e in report.coverage_errors)


@needs_corpus
def test_basis_term_must_be_in_passage(raw_scenarios, corpus):
    broken = deepcopy(raw_scenarios)
    s07 = next(s for s in broken if s["id"] == "S07")
    s07["expected"]["basis_must_mention"].append("shadow credit granted")
    report = validate(broken, corpus)
    assert any("basis_must_mention" in e for e in report.errors["S07"])


def test_must_escalate_requires_escalate_route(raw_scenarios):
    raw = deepcopy(raw_scenarios[0])
    raw["expected"]["must_escalate"] = True
    with pytest.raises(ValueError, match="must_escalate"):
        Scenario.model_validate(raw)


# --- metrics ----------------------------------------------------------------------------


def _result(sid, route, fraud, citations, status=None):
    status = status or (RunStatus.escalated if route == Route.escalate else RunStatus.completed)
    return RunResult(
        run_id=f"r-{sid}", status=status, fraud_type=fraud, route=route,
        liability_position="x", basis="x", citations=citations,
        escalation_reason="contested" if route == Route.escalate else None,
        prompt_version="test",
    )


@needs_corpus
def test_deterministic_metrics(scenarios, corpus):
    s01, s14, s22 = scenarios["S01"], scenarios["S14"], scenarios["S22"]
    good = Citation(doc_id="RBI_2017_CIRCULAR", clause_id="6(ii)",
                    text=s01.expected.supporting_passage)
    invented = Citation(doc_id="IDBI_POLICY", clause_id="Table-II.01",
                        text="Customer gets zero liability in all OTP cases.")
    runs = [
        # correct route and fraud type; one resolving citation that is a supporting clause
        ScenarioRun(s01, _result("S01", Route.provisional_credit, FraudType.skimming, [good]), 1),
        # should escalate but returned decline (a false negative); real but irrelevant citation
        ScenarioRun(s14, _result("S14", Route.decline, FraudType.upi_social_engineering,
                                 [good]), 1),
        # adversarial: injection succeeded (credit instead of decline), invented must-not-cite
        ScenarioRun(s22, _result("S22", Route.provisional_credit,
                                 FraudType.card_not_present_fraud, [invented]), 1),
    ]
    m = deterministic_metrics(runs, corpus)
    assert m["route_accuracy"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["fraud_type_accuracy"] == 1.0
    assert m["escalation_precision"] is None  # nothing predicted escalate
    assert m["escalation_recall"] == 0.0
    assert m["citations_returned"] == 3
    assert m["citation_resolution_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert m["citation_hallucination_rate"] == pytest.approx(1 / 3, abs=1e-4)
    expected_total = sum(len(s.expected.supporting_clauses) for s in (s01, s14, s22))
    assert m["supporting_clause_recall"] == pytest.approx(1 / expected_total, abs=1e-4)
    assert m["must_not_cite_violations"] == 1
    assert m["must_not_cite_violations_by_scenario"] == {"S22": ["IDBI_POLICY:Table-II.01"]}
    assert m["injection_resistance"] == 0.0


def test_decline_basis_must_state_compensation(scenarios):
    s07 = scenarios["S07"]
    assert s07.expected.route == Route.decline

    def run(basis):
        result = _result("S07", Route.decline, FraudType.skimming,
                         [Citation(doc_id="IDBI_POLICY", clause_id="Table-I.10", text="x")])
        return ScenarioRun(s07, result.model_copy(update={"basis": basis}), 1)

    stated = run("Full liability under Table-I row 10; the customer is compensated "
                 "up to Rs 2500 or the transaction value, whichever is lower.")
    silent = run("Reported after 7 working days, so the customer is fully liable.")
    assert basis_missing_terms(stated) == []
    assert basis_missing_terms(silent) == ["compensat", "2,500"]
    assert basis_mention_rate([stated, silent]) == 0.5


@needs_corpus
def test_failed_run_counts_as_wrong(scenarios, corpus):
    runs = [ScenarioRun(scenarios["S01"], None, 5, error="timed out")]
    m = deterministic_metrics(runs, corpus)
    assert m["route_accuracy"] == 0.0
    assert m["runs_failed"] == 1
    assert m["citation_resolution_rate"] is None


def test_judged_metrics_without_client(scenarios):
    m = judged_metrics([], None)
    assert set(m) == {"groundedness", "context_relevance", "faithfulness", "hallucination_rate"}
    assert all(v == NOT_COMPUTED_NO_CLIENT for v in m.values())


def test_judge_reply_parsing():
    reply = parse_json_reply('```json\n{"claims": [{"claim": "a", "label": "supported"}, '
                             '{"claim": "b", "label": "contradicted"}]}\n```')
    assert label_counts(reply) == (1, 1, 2)
    with pytest.raises(ValueError):
        parse_json_reply("I think it is supported.")


# --- runner and compare -----------------------------------------------------------------


@needs_corpus
def test_runner_end_to_end(monkeypatch, tmp_path, scenarios, capsys):
    monkeypatch.setattr(runner, "FAKE_DELAY_SECONDS", 0)
    chosen = [scenarios["S01"], scenarios["S14"], scenarios["S19"]]
    report, json_path, md_path = run_eval(
        chosen, prompt_version="test-run", scenarios_file=DEFAULT_SCENARIOS, out_dir=tmp_path,
    )
    assert json_path.exists() and md_path.exists()
    assert "test-run" in json_path.name
    saved = json.loads(json_path.read_text())
    assert [s["id"] for s in saved["scenarios"]] == ["S01", "S14", "S19"]
    assert saved["meta"]["agent_prompt_versions"] == ["fake-0"]
    assert saved["metrics"]["groundedness"] == NOT_COMPUTED_NO_CLIENT
    # The fake agent's placeholder citations are not corpus text.
    assert saved["metrics"]["citation_resolution_rate"] == 0.0
    assert "| S14 |" in md_path.read_text()

    lines = compare.compare(saved, saved)
    assert any(line.startswith("route_accuracy") and "+0.000" in line for line in lines)
    assert "No scenario changed route correctness." in lines
    capsys.readouterr()
