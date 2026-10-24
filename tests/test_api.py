from rq import Queue
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from common.db import Quarantine, Run


def _count(engine, model) -> int:
    with Session(engine) as s:
        return s.scalar(select(func.count()).select_from(model))


def test_malformed_post_is_quarantined_and_not_enqueued(client, redis, engine, payload):
    queue = Queue("disputes", connection=redis)
    before = queue.count
    payload["transaction_amount_inr"] = -5
    payload["reported_datetime"] = "2026-08-01T00:00:00+05:30"

    res = client.post("/disputes", json=payload)

    assert res.status_code == 422
    body = res.json()
    locs = [tuple(e["loc"]) for e in body["detail"]]
    assert ("transaction_amount_inr",) in locs
    assert body["quarantine_id"] >= 1
    assert queue.count == before == 0
    assert _count(engine, Run) == 0
    assert _count(engine, Quarantine) == 1

    listed = client.get("/quarantine").json()
    assert listed[0]["id"] == body["quarantine_id"]
    assert listed[0]["errors"] == body["detail"]


def test_non_json_body_is_quarantined(client, redis, engine):
    res = client.post(
        "/disputes", content=b"not json", headers={"Content-Type": "application/json"}
    )
    assert res.status_code == 422
    assert Queue("disputes", connection=redis).count == 0
    assert client.get("/quarantine").json()[0]["raw_payload"] == "not json"


def test_valid_post_returns_202_and_enqueues(client, redis, payload):
    res = client.post("/disputes", json=payload, headers={"X-Correlation-ID": "test-corr-1"})

    assert res.status_code == 202
    body = res.json()
    assert body["status"] == "queued"
    assert body["status_url"] == f"/runs/{body['run_id']}"
    assert res.headers["X-Correlation-ID"] == "test-corr-1"

    queue = Queue("disputes", connection=redis)
    assert queue.job_ids == [body["run_id"]]
    assert queue.fetch_job(body["run_id"]).args == (body["run_id"], "test-corr-1")

    run = client.get(body["status_url"]).json()
    assert run["status"] == "queued"
    assert run["result"] is None


def test_unknown_run_is_404(client):
    assert client.get("/runs/does-not-exist").status_code == 404


def test_feedback_persisted(client, payload):
    run_id = client.post("/disputes", json=payload).json()["run_id"]
    res = client.post(
        f"/runs/{run_id}/feedback", json={"satisfactory": False, "reason": "wrong route"}
    )
    assert res.status_code == 201
    assert res.json()["run_id"] == run_id
    assert client.post(
        "/runs/nope/feedback", json={"satisfactory": True, "reason": "x"}
    ).status_code == 404


def test_health_ready_metrics(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    ready = client.get("/readyz")
    assert ready.status_code == 200
    assert ready.json()["checks"] == {"redis": "ok", "postgres": "ok"}
    metrics = client.get("/metrics").text
    assert "http_requests_total" in metrics


def test_correlation_id_generated_when_missing(client):
    assert client.get("/healthz").headers["X-Correlation-ID"]


def test_openapi_publishes_contract(client):
    schema = client.get("/openapi.json").json()
    assert "DisputeRequest" in schema["components"]["schemas"]
