"""Measure the effect of the duplicate cache and per-client rate limiting (D7) on a running
stack. Results and the exact setup used are in docs/api_controls.md.

  python scripts/measure_api_controls.py cache  [--submissions 40 --distinct 10 --concurrency 4]
  python scripts/measure_api_controls.py noisy  [--seconds 60]

cache  Submits --submissions disputes drawn (fixed seed) from --distinct different ones, as
       a retrying client would, from --concurrency threads, and waits for each run to
       finish. Reports end-to-end latency (POST -> terminal status) p50/p95, runs started,
       cache hits, and the model calls, tokens and cost recorded in llm_calls meanwhile
       (read with `docker compose exec postgres psql`; skip with --no-llm-stats).
noisy  One client floods POST /disputes as fast as it can while a second, well-behaved
       client submits one dispute every --victim-interval seconds (default 7, under
       the 10/min limit used in docs/api_controls.md) and waits for it. Reports the well-behaved
       client's p50/p95 and how many of the flood's requests got 202 and 429.

Run each twice, with the control on and off (CACHE_TTL_SECONDS=0 / RATE_LIMIT_PER_MINUTE=0
when starting the api), and compare. `noisy` tells its two clients apart by X-Client-ID, so
start the api with RATE_LIMIT_TRUST_CLIENT_ID=true for it.
"""

import argparse
import json
import math
import random
import statistics
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import httpx

TERMINAL = {"completed", "escalated", "failed"}


def dispute(n: int) -> dict:
    """The n-th distinct dispute (same n -> identical payload)."""
    day = datetime(2026, 10, 1, 10, 0, tzinfo=UTC) + timedelta(days=n % 20)
    return {
        "customer_id": f"CUST-M{n:05d}",
        "account_type": "SAVINGS",
        "channel": "CARD_NOT_PRESENT",
        "transaction_id": f"TXN-M-{n:06d}",
        "transaction_amount_inr": 1000.0 + n,
        "transaction_datetime": day.isoformat(),
        "bank_alert_datetime": (day + timedelta(minutes=1)).isoformat(),
        "reported_datetime": (day + timedelta(hours=20)).isoformat(),
        "home_branch_state": "Karnataka",
        "complaint_text": f"An online purchase of Rs {1000 + n} I did not make appeared on my "
                          "card. I still have the card and shared no OTP.",
        "customer_claims": {"shared_otp_or_pin": False, "card_in_possession": True,
                            "received_alert": True},
    }


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # Nearest rank: the smallest value with at least p% of the values at or below it.
    k = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return round(ordered[k], 1)


def submit_and_wait(client: httpx.Client, payload: dict, client_id: str,
                    timeout_s: float = 180) -> dict:
    started = time.perf_counter()
    res = client.post("/disputes", json=payload, headers={"X-Client-ID": client_id})
    out = {"code": res.status_code, "cache": res.headers.get("X-Cache")}
    if res.status_code != 202:
        return out
    run_id = res.json()["run_id"]
    status = None
    while time.perf_counter() - started < timeout_s:
        status = client.get(f"/runs/{run_id}").json()["status"]
        if status in TERMINAL:
            break
        time.sleep(0.25)
    out.update(run_id=run_id, status=status, ms=(time.perf_counter() - started) * 1000)
    return out


def llm_stats(since: datetime) -> dict | None:
    sql = ("select count(*), coalesce(sum(prompt_tokens + completion_tokens), 0), "
           "coalesce(sum(cost_usd), 0) from llm_calls where created_at >= "
           f"'{since.isoformat()}' and step not like 'judge%'")
    cmd = ["docker", "compose", "exec", "-T", "postgres", "sh", "-c",
           f'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F, -c "{sql}"']
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        return {"error": str(exc)}
    calls, tokens, cost = out.split(",")
    return {"llm_calls": int(calls), "tokens": int(tokens), "cost_usd": float(cost)}


def run_cache(args) -> dict:
    rng = random.Random(7)
    picks = [rng.randrange(args.distinct) for _ in range(args.submissions)]
    base = args.offset if args.offset is not None else random.randrange(10**6)
    since = datetime.now(UTC)
    with httpx.Client(base_url=args.host, timeout=30) as client, \
            ThreadPoolExecutor(args.concurrency) as pool:
        results = list(pool.map(
            lambda i_n: submit_and_wait(client, dispute(base + i_n[1]), f"measure-{i_n[0]}"),
            enumerate(picks)))
    time.sleep(1)
    latencies = [r["ms"] for r in results if "ms" in r]
    report = {
        "experiment": "cache", "submissions": args.submissions, "distinct": args.distinct,
        "concurrency": args.concurrency,
        "runs_started": len({r["run_id"] for r in results if "run_id" in r}),
        "cache_hits": sum(r.get("cache") == "HIT" for r in results),
        "e2e_p50_ms": percentile(latencies, 50), "e2e_p95_ms": percentile(latencies, 95),
        "e2e_mean_ms": round(statistics.mean(latencies), 1) if latencies else None,
        "statuses": {s: sum(r.get("status") == s for r in results) for s in TERMINAL},
    }
    if not args.no_llm_stats:
        report["llm"] = llm_stats(since)
    return report


def run_noisy(args) -> dict:
    stop = threading.Event()
    flood = {"202": 0, "429": 0, "other": 0}

    def flooder():
        with httpx.Client(base_url=args.host, timeout=30) as client:
            while not stop.is_set():
                payload = dispute(random.randrange(10**7))
                payload["transaction_id"] = f"TXN-F-{uuid.uuid4().hex[:12]}"
                code = client.post("/disputes", json=payload,
                                   headers={"X-Client-ID": "noisy"}).status_code
                flood[str(code) if str(code) in flood else "other"] += 1

    threads = [threading.Thread(target=flooder, daemon=True) for _ in range(args.flooders)]
    for t in threads:
        t.start()
    victim = []
    started = time.perf_counter()
    with httpx.Client(base_url=args.host, timeout=30) as client:
        while time.perf_counter() - started < args.seconds:
            payload = dispute(random.randrange(10**7))
            payload["transaction_id"] = f"TXN-V-{uuid.uuid4().hex[:12]}"
            t0 = time.perf_counter()
            victim.append(submit_and_wait(client, payload, "well-behaved"))
            time.sleep(max(0.0, args.victim_interval - (time.perf_counter() - t0)))
    stop.set()
    latencies = [r["ms"] for r in victim if "ms" in r]
    return {
        "experiment": "noisy", "seconds": args.seconds, "flooders": args.flooders,
        "victim_interval_s": args.victim_interval,
        "flood_requests": flood,
        "victim_runs": len(latencies),
        "victim_refused": sum(r["code"] != 202 for r in victim),
        "victim_p50_ms": percentile(latencies, 50), "victim_p95_ms": percentile(latencies, 95),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("experiment", choices=["cache", "noisy"])
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--label", default="", help="free text stored in the output")
    parser.add_argument("--submissions", type=int, default=40)
    parser.add_argument("--distinct", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--offset", type=int, help="first dispute number (default random, "
                        "so earlier runs are never cache hits)")
    parser.add_argument("--no-llm-stats", action="store_true")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--flooders", type=int, default=1)
    parser.add_argument("--victim-interval", type=float, default=7)
    args = parser.parse_args()
    report = run_cache(args) if args.experiment == "cache" else run_noisy(args)
    report["label"] = args.label
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
