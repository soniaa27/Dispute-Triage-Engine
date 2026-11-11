"""Load test (requirements D6, A7; target in docs/slo.md, results in docs/load_test.md).

    pip install -r loadtest/requirements.txt
    make loadtest-up WORKERS=1                    # stack with the agent-aware stub
    make loadtest NAME=a_1worker STEPS=2:600      # headless, CSV in loadtest/results/

What is measured: each simulated analyst submits a dispute and polls GET /runs/{id} until a
final status. The time from just before the POST to the poll that sees completed, escalated
or failed is recorded as the request "RUN submit to final status" (type RUN): that is the
SLO's measure. The POST itself is recorded separately ("POST /disputes").

Arrival rate, not concurrency, is the load: every user submits once every
LOAD_PACING_SECONDS whatever the system does (open loop), and polls each run in its own
greenlet. So users x (1 / pacing) is the offered rate, and slow runs do not slow the
arrivals, as with real analysts. LOAD_STEPS ramps the users in steps, "users:seconds,...".
A final drain stage (LOAD_DRAIN_SECONDS, no new submissions) lets runs in flight finish.

Disputes come from the 24 evaluation scenarios in turn, each with a fresh transaction id so
the duplicate cache never answers instead of a run. Each user sends its own X-Client-ID
(the stack must trust it: make loadtest-up sets RATE_LIMIT_TRUST_CLIENT_ID=true), so the
per-client limit treats users as separate analysts.

At the end a summary per step (offered rate, submissions, statuses, p50, p95, whether the
SLO was met) is printed and written to LOAD_SUMMARY. Runs still unfinished at the end count
as slower than any finished run, so they push p95 up rather than vanish.
"""

import itertools
import json
import math
import os
import time
import uuid
from copy import deepcopy
from pathlib import Path

import gevent
from locust import HttpUser, LoadTestShape, constant_pacing, events, task
from urllib3 import PoolManager

SCENARIOS = Path(__file__).resolve().parent.parent / "eval" / "scenarios.jsonl"
TERMINAL = {"completed", "escalated", "failed"}
SLO_P95_SECONDS = 60.0     # docs/slo.md
SLO_FAILED_SHARE = 0.01    # docs/slo.md

PACING = float(os.environ.get("LOAD_PACING_SECONDS", "10"))
POLL = float(os.environ.get("LOAD_POLL_SECONDS", "1"))
GIVE_UP = float(os.environ.get("LOAD_GIVE_UP_SECONDS", "900"))
STEPS = [(int(u), float(s)) for u, s in (
    part.split(":") for part in os.environ.get("LOAD_STEPS", "1:300").split(","))]
DRAIN = float(os.environ.get("LOAD_DRAIN_SECONDS", "180"))
SUMMARY = os.environ.get("LOAD_SUMMARY")


def _pool() -> list[tuple[str, dict]]:
    with SCENARIOS.open(encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return [(s["id"], s["request"]) for s in sorted(rows, key=lambda s: s["id"])]


_next_dispute = itertools.cycle(_pool()).__next__

# Per run: step index at submission, scenario, seconds to final status (None = unfinished),
# final status.
RUNS: list[dict] = []
_stage = {"index": 0}


class StepShape(LoadTestShape):
    """LOAD_STEPS, then a drain stage with no users so runs in flight can finish."""

    def tick(self):
        elapsed = self.get_run_time()
        for index, (users, seconds) in enumerate(STEPS):
            if elapsed < seconds:
                _stage["index"] = index
                return users, max(users, 1)
            elapsed -= seconds
        _stage["index"] = len(STEPS)
        if elapsed < DRAIN and any(r["status"] is None for r in RUNS):
            return 0, 1
        return None


class Analyst(HttpUser):
    wait_time = constant_pacing(PACING)
    # One poller per run in flight shares the user's connections; the default 10 is too few.
    pool_manager = PoolManager(maxsize=100, block=False)

    def on_start(self):
        self.client_id = f"load-{uuid.uuid4().hex[:12]}"

    @task
    def submit(self):
        scenario_id, request = _next_dispute()
        body = deepcopy(request)
        body["transaction_id"] = f"TXN-LOAD-{uuid.uuid4().hex[:16].upper()}"
        started = time.perf_counter()
        with self.client.post("/disputes", json=body, name="POST /disputes",
                              headers={"X-Client-ID": self.client_id},
                              catch_response=True) as res:
            if res.status_code != 202:
                res.failure(f"HTTP {res.status_code}: {res.text[:120]}")
                return
            run_id = res.json()["run_id"]
        run = {"stage": _stage["index"], "scenario": scenario_id, "run_id": run_id,
               "seconds": None, "status": None}
        RUNS.append(run)
        gevent.spawn(self._poll, run, started)

    def _poll(self, run: dict, started: float) -> None:
        url = f"/runs/{run['run_id']}"
        while time.perf_counter() - started < GIVE_UP:
            res = self.client.get(url, name="GET /runs/[run_id]")
            status = res.json().get("status") if res.status_code == 200 else None
            if status in TERMINAL:
                run["seconds"] = time.perf_counter() - started
                run["status"] = status
                self.environment.events.request.fire(
                    request_type="RUN", name="submit to final status",
                    response_time=run["seconds"] * 1000, response_length=0, context={},
                    exception=RuntimeError("run failed") if status == "failed" else None)
                return
            gevent.sleep(POLL)
        run["status"] = "gave_up"


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; unfinished runs are passed as math.inf."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summarise(runs: list[dict]) -> list[dict]:
    out = []
    for index, (users, seconds) in enumerate(STEPS):
        rows = [r for r in runs if r["stage"] == index]
        times = [r["seconds"] if r["seconds"] is not None else math.inf for r in rows]
        statuses: dict[str, int] = {}
        for r in rows:
            key = r["status"] or "unfinished"
            statuses[key] = statuses.get(key, 0) + 1
        p50, p95 = _percentile(times, 0.50), _percentile(times, 0.95)
        failed = statuses.get("failed", 0) / len(rows) if rows else 0.0
        out.append({
            "step": index + 1, "users": users, "seconds": seconds,
            "offered_rate_per_s": round(users / PACING, 3),
            "submitted": len(rows), "statuses": statuses,
            "p50_s": None if p50 is None or p50 == math.inf else round(p50, 1),
            "p95_s": None if p95 is None else ("unfinished" if p95 == math.inf
                                                else round(p95, 1)),
            "failed_share": round(failed, 4),
            "slo_met": bool(rows) and p95 is not None and p95 <= SLO_P95_SECONDS
                       and failed < SLO_FAILED_SHARE,
        })
    return out


@events.test_stop.add_listener
def _write_summary(environment, **_):
    steps = summarise(RUNS)
    for s in steps:
        print(f"step {s['step']}: {s['users']} users = {s['offered_rate_per_s']}/s for "
              f"{s['seconds']:g}s, {s['submitted']} runs {s['statuses']}, p50 {s['p50_s']} s, "
              f"p95 {s['p95_s']} s, SLO {'met' if s['slo_met'] else 'NOT met'}")
    if SUMMARY:
        Path(SUMMARY).parent.mkdir(parents=True, exist_ok=True)
        Path(SUMMARY).write_text(json.dumps({
            "pacing_seconds": PACING, "poll_seconds": POLL, "steps": steps,
            "runs": RUNS}, indent=2) + "\n")
