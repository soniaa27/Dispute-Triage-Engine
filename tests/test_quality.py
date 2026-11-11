"""Quality over time (L6): sampling, the live judge pass, and GET /quality."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from common.db import Feedback, QualityScore, Run, make_session_factory
from common.quality import summarize
from contracts import Citation, Route, RunResult, RunStatus
from contracts.llm import LLMUnavailableError
from quality import QualitySettings, in_sample, score_once
from tests.agent_doubles import ScriptedLLM, scenario_request

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
CITATIONS = [Citation(doc_id="IDBI_POLICY", clause_id="Table-I.01", text="Zero liability."),
             Citation(doc_id="RBI_2017_CIRCULAR", clause_id="6(i)", text="Zero liability.")]
# One run's judge: claims, one relevance reply per citation, faithfulness.
JUDGE_REPLIES = [
    {"claims": [{"claim": "a", "label": "supported"}, {"claim": "b", "label": "supported"},
                {"claim": "c", "label": "unsupported"}, {"claim": "d", "label": "supported"}]},
    {"relevance": 1.0}, {"relevance": 0.5},
    {"faithful": True, "reason": "follows"},
]


def _settings(database_url, **overrides) -> QualitySettings:
    values = {"quality_sample_rate": 1.0, "quality_max_per_pass": 5, **overrides}
    return QualitySettings(database_url=database_url, **values)


def _add_run(engine, run_id, *, status=RunStatus.completed, citations=CITATIONS,
             finished=NOW - timedelta(hours=1), prompt_version="inv-v1+ver-v1"):
    if status == RunStatus.completed:
        result = RunResult(run_id=run_id, status=status, route=Route.provisional_credit,
                           liability_position="Zero liability.", basis="Reported in time.",
                           citations=citations, prompt_version=prompt_version)
    else:
        result = RunResult(run_id=run_id, status=status, route=Route.escalate,
                           escalation_reason="model unavailable", citations=citations,
                           prompt_version=prompt_version)
    with make_session_factory(engine).begin() as session:
        session.add(Run(id=run_id, status=status, correlation_id="c",
                        request_json=scenario_request("S01").model_dump(mode="json"),
                        result_json=result.model_dump(mode="json"),
                        created_at=finished, updated_at=finished))


def _scores(engine) -> list[QualityScore]:
    with make_session_factory(engine)() as session:
        return session.scalars(select(QualityScore)).all()


def test_sampling_is_deterministic_and_close_to_the_rate():
    ids = [f"run-{i}" for i in range(4000)]
    picked = [i for i in ids if in_sample(i, 0.2)]
    assert picked == [i for i in ids if in_sample(i, 0.2)]
    assert 0.17 < len(picked) / len(ids) < 0.23
    assert not any(in_sample(i, 0.0) for i in ids) and all(in_sample(i, 1.0) for i in ids)


def test_pass_scores_a_run_with_the_eval_judge(engine, database_url):
    _add_run(engine, "run-a")
    llm = ScriptedLLM(list(JUDGE_REPLIES))
    report = score_once(make_session_factory(engine), llm, _settings(database_url), now=NOW)

    assert (report.candidates, report.scored, report.errors) == (1, 1, 0)
    assert llm.steps == ["judge_claims", "judge_relevance", "judge_relevance",
                         "judge_faithfulness"]
    [row] = _scores(engine)
    assert row.run_id == "run-a" and row.claims == 4 and row.groundedness == 0.75
    assert row.hallucination_rate == 0.25 and row.context_relevance == 0.75
    assert row.faithful is True and row.judge_model == "scripted" and row.error is None

    # Scored once: the next pass finds nothing to do and makes no call.
    again = score_once(make_session_factory(engine), ScriptedLLM([]), _settings(database_url),
                       now=NOW)
    assert again.candidates == 0


def test_runs_without_citations_old_runs_and_unsampled_runs_are_skipped(engine, database_url):
    _add_run(engine, "no-cites", status=RunStatus.escalated, citations=[])
    _add_run(engine, "too-old", finished=NOW - timedelta(days=3))
    report = score_once(make_session_factory(engine), ScriptedLLM([]),
                        _settings(database_url), now=NOW)
    assert report.candidates == 0
    _add_run(engine, "sampled-out")
    report = score_once(make_session_factory(engine), ScriptedLLM([]),
                        _settings(database_url, quality_sample_rate=0.0), now=NOW)
    assert report.candidates == 0


def test_max_per_pass_limits_judge_calls(engine, database_url):
    for i in range(3):
        _add_run(engine, f"run-{i}")
    report = score_once(make_session_factory(engine), ScriptedLLM(list(JUDGE_REPLIES)),
                        _settings(database_url, quality_max_per_pass=1), now=NOW)
    assert (report.candidates, report.scored) == (3, 1)


def test_model_unavailable_stops_the_pass_without_writing(engine, database_url):
    _add_run(engine, "run-a")
    report = score_once(make_session_factory(engine),
                        ScriptedLLM([LLMUnavailableError("breaker open")]),
                        _settings(database_url), now=NOW)
    assert report.stopped.startswith("model unavailable") and _scores(engine) == []


def test_unusable_judge_reply_is_recorded_once(engine, database_url):
    _add_run(engine, "run-a")
    report = score_once(make_session_factory(engine), ScriptedLLM(["not json"]),
                        _settings(database_url), now=NOW)
    [row] = _scores(engine)
    assert report.errors == 1 and "not JSON" in row.error and row.groundedness is None


def test_wrongly_typed_judge_reply_is_recorded_not_retried(engine, database_url):
    _add_run(engine, "run-a")
    replies = [JUDGE_REPLIES[0], {"relevance": "high"}]
    report = score_once(make_session_factory(engine), ScriptedLLM(replies),
                        _settings(database_url), now=NOW)
    [row] = _scores(engine)
    assert report.errors == 1 and row.error.startswith("ValueError")


def test_summary_buckets_and_feedback(engine, database_url):
    factory = make_session_factory(engine)
    _add_run(engine, "day1", finished=NOW - timedelta(days=1, hours=2))
    _add_run(engine, "day0", finished=NOW - timedelta(hours=2), prompt_version="inv-v2+ver-v1")
    score_once(factory, ScriptedLLM(JUDGE_REPLIES * 2),
               _settings(database_url, quality_lookback_hours=48), now=NOW)
    with factory.begin() as session:
        session.add_all([Feedback(run_id="day0", satisfactory=True, reason="", created_at=NOW),
                         Feedback(run_id="day0", satisfactory=False, reason="",
                                  created_at=NOW)])

    summary = summarize(factory, days=2, bucket="day", now=NOW)
    assert [b["start"][:10] for b in summary["buckets"]] == ["2026-11-08", "2026-11-09",
                                                             "2026-11-10"]
    assert [b["runs_scored"] for b in summary["buckets"]] == [0, 1, 1]
    assert summary["buckets"][0]["groundedness"] is None
    assert summary["buckets"][2]["satisfactory_rate"] == 0.5
    assert summary["overall"]["groundedness"] == 0.75
    assert set(summary["by_prompt_version"]) == {"inv-v1+ver-v1", "inv-v2+ver-v1"}
    assert summary["recent"][0]["run_id"] == "day0"


def test_quality_endpoint(client):
    body = client.get("/quality?days=3&bucket=hour").json()
    assert body["bucket"] == "hour" and len(body["buckets"]) >= 72
    assert body["overall"]["runs_scored"] == 0
    assert client.get("/quality?days=30&bucket=hour").status_code == 422
