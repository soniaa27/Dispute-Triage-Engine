import time

import pytest
from sqlalchemy.orm import Session

from agent import runner
from common.db import Run
from contracts import DisputeRequest
from worker import tasks
from worker.settings import WorkerSettings


@pytest.fixture
def worker_env(monkeypatch, database_url, engine):
    monkeypatch.setattr(runner, "FAKE_DELAY_SECONDS", 0)
    tasks.get_settings.cache_clear()
    tasks.get_session_factory.cache_clear()
    settings = WorkerSettings(database_url=database_url, redis_url="redis://unused")
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    yield settings
    tasks.get_session_factory.cache_clear()


def _make_run(engine, payload, run_id="run-1"):
    req = DisputeRequest.model_validate(payload)
    with Session(engine) as s, s.begin():
        s.add(Run(id=run_id, status="queued", correlation_id="c", request_json=req.model_dump(
            mode="json")))
    return run_id


def _load(engine, run_id) -> Run:
    with Session(engine) as s:
        return s.get(Run, run_id)


def test_process_run_completed(worker_env, engine, payload):
    run_id = _make_run(engine, payload)
    assert tasks.process_run(run_id, "c") == "completed"
    run = _load(engine, run_id)
    assert run.status == "completed"
    assert run.result_json["route"] == "provisional_credit"
    assert run.result_json["citations"][0]["doc_id"] == "RBI_2017_CIRCULAR"
    assert run.result_json["prompt_version"] == "fake-0"


def test_process_run_escalates_on_otp(worker_env, engine, payload):
    payload["complaint_text"] = "I shared an OTP with a caller claiming to be the bank."
    run_id = _make_run(engine, payload)
    assert tasks.process_run(run_id, "c") == "escalated"
    run = _load(engine, run_id)
    assert run.result_json["route"] == "escalate"
    assert run.result_json["escalation_reason"]
    assert {c["doc_id"] for c in run.result_json["citations"]} == {"RBI_2017_CIRCULAR",
                                                                   "IDBI_POLICY"}


def test_process_run_timeout_marks_failed(worker_env, engine, payload, monkeypatch):
    monkeypatch.setattr(worker_env, "run_timeout_seconds", 0.1)
    monkeypatch.setattr(tasks, "run_agent", lambda req, run_id: time.sleep(1))
    run_id = _make_run(engine, payload)
    assert tasks.process_run(run_id, "c") == "failed"
    run = _load(engine, run_id)
    assert run.status == "failed"
    assert "RUN_TIMEOUT_SECONDS" in run.error
    assert run.result_json is None


def test_process_run_agent_exception_marks_failed(worker_env, engine, payload, monkeypatch):
    def boom(req, run_id):
        raise RuntimeError("provider down")

    monkeypatch.setattr(tasks, "run_agent", boom)
    run_id = _make_run(engine, payload)
    assert tasks.process_run(run_id, "c") == "failed"
    assert "provider down" in _load(engine, run_id).error
