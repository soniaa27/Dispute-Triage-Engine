"""Load test stub (requirement D6).  pip install -r loadtest/requirements.txt

    locust -f loadtest/locustfile.py --host http://localhost:8000

TODO: declare the p95 SLO in docs/decisions.md BEFORE running; measure submission ->
terminal status (not just the POST); run with LLM_MODE=stub.
"""

import time
from datetime import UTC, datetime, timedelta

from locust import HttpUser, between, task

TERMINAL = {"completed", "escalated", "failed"}


def _payload() -> dict:
    now = datetime.now(UTC)
    return {
        "customer_id": "CUST-LOAD-01",
        "account_type": "SAVINGS",
        "channel": "CARD_NOT_PRESENT",
        "transaction_id": "TXN-LOAD-0001",
        "transaction_amount_inr": 999.0,
        "transaction_datetime": (now - timedelta(days=2)).isoformat(),
        "reported_datetime": (now - timedelta(days=1)).isoformat(),
        "home_branch_state": "Karnataka",
        "complaint_text": "Load test: an online transaction I did not make.",
    }


class DisputeUser(HttpUser):
    wait_time = between(1, 3)

    @task
    def submit_and_poll(self):
        started = time.perf_counter()
        res = self.client.post("/disputes", json=_payload())
        if res.status_code != 202:
            return
        status_url = res.json()["status_url"]
        for _ in range(120):
            status = self.client.get(status_url, name="/runs/[run_id]").json()["status"]
            if status in TERMINAL:
                break
            time.sleep(1)
        self.environment.events.request.fire(
            request_type="RUN",
            name="end_to_end",
            response_time=(time.perf_counter() - started) * 1000,
            response_length=0,
            exception=None if status in TERMINAL else TimeoutError("run did not finish"),
            context={},
        )
