import pytest

from common.db import LlmCall, init_db, make_engine, make_session_factory
from scripts.cost_report import build_report, format_report


def add_calls(url, rows):
    engine = make_engine(url)
    init_db(engine)
    with make_session_factory(engine)() as session, session.begin():
        for run_id, step, p, c, cost, outcome in rows:
            session.add(LlmCall(run_id=run_id, correlation_id="c", step=step,
                                prompt_version="v1", provider="gemini", model="m",
                                prompt_tokens=p, completion_tokens=c, cost_usd=cost,
                                latency_ms=100, attempts=1, outcome=outcome))


def test_report_per_step_per_run_and_driver(tmp_path):
    url = f"sqlite:///{tmp_path / 'c.db'}"
    add_calls(url, [
        ("r1", "investigator", 1000, 200, 0.03, "ok"),
        ("r1", "verifier", 500, 100, 0.01, "ok"),
        ("r2", "investigator", 1200, 300, 0.04, "retried_ok"),
        ("r2", "verifier", 0, 0, 0.0, "breaker_open"),
    ])
    report = build_report(url)
    assert report["runs"] == 2 and report["calls"] == 4
    assert report["total_cost_usd"] == pytest.approx(0.08)
    assert report["cost_per_100_runs_usd"] == pytest.approx(4.0)
    assert report["largest_cost_driver"]["step"] == "investigator"
    assert report["largest_cost_driver"]["measure"] == "cost_usd"
    text = format_report(report)
    assert "Per 100 runs: $4.0000" in text and "breaker_open" in text


def test_free_tier_driver_falls_back_to_tokens(tmp_path):
    url = f"sqlite:///{tmp_path / 'c.db'}"
    add_calls(url, [("r1", "investigator", 100, 10, 0.0, "ok"),
                    ("r1", "judge_claims", 900, 50, 0.0, "ok")])
    driver = build_report(url)["largest_cost_driver"]
    assert driver == {"step": "judge_claims", "measure": "tokens", "value": 950}
