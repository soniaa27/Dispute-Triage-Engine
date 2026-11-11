# Load test

Requirements D6 and A7. The target was declared in [slo.md](slo.md) before any test; this
page reports what was measured against it on 11 November 2026.

## The SLO as declared

Quoted from [slo.md](slo.md), unchanged:

> - **Latency:** p95 time for a complete run of at most 60 seconds, measured
>   from POST /disputes to a final status (completed, escalated or failed)
>   visible via GET /runs/{run_id}.
> - **Load:** a sustained arrival rate of 0.1 submissions per second
>   (one every 10 seconds).
> - **Errors:** fewer than 1% of runs end as failed.
> - **Availability:** at least 99% of submissions accepted (HTTP 202, or 422
>   for invalid input), none lost.
>
> Test conditions: the LLM is replaced by the stub provider: correctly formatted replies for
> each agent call after a fixed delay of 4 seconds per call [...] The stub is exempt from the
> free-tier rate limiter during the test. Measured from submission to final result, not the
> submission call alone. The worker count is a test variable: 1 worker, then 3.

## Result in one paragraph

**At the declared load the SLO is met with 3 workers (p95 18.5 s) and not with 1 (p95
316.8 s).** No run failed and every submission was accepted in any test. One worker keeps
the target up to 0.05 submissions/s and fails at 0.075/s; three workers keep it up to
0.15/s and fail at 0.2/s. In both cases the limit is the number of workers: each runs one
dispute at a time and spends about 95% of it waiting for the model, while CPU, memory, the
API, Postgres, Redis and the gateway stay far from their limits. Scaling from 1 to 3
workers raised the sustainable rate about threefold. At 0.3/s with three workers, the API's
single process started to slow down under the clients' polling: that is the next limit.

## Setup

| Item | Value |
|------|-------|
| Machine | MacBook Air, Apple M1, 8 GB. Docker Desktop VM: 8 CPUs, 3.8 GB. The observability stack (Prometheus, Grafana, Jaeger, cAdvisor) ran on the same machine |
| Stack | `make loadtest-up WORKERS=n`: `LLM_PROVIDER=stub`, `STUB_DELAY_MS=4000`, `AGENT_MODE=graph`, `RATE_LIMIT_TRUST_CLIENT_ID=true`, the stub's own limiter budget of 6000/min ([D-035](decisions.md)). Agent limits from the local `.env`: wall clock 120 s, 40 tool calls, 12 steps, `RUN_TIMEOUT_SECONDS=175` |
| Stub | Agent-aware ([D-035](decisions.md)): every agent call gets a reply the agent parses, after 4 s. The graph runs as with a real model: two Investigator turns, the Verifier's citation and linked-clause checks, its conflict search and judge, hard rules |
| Workload | The 24 evaluation scenarios in turn, each with a fresh transaction id (no cache hits). 15 complete after 4 model calls (about 17 s), 9 escalate after 2 (about 8 s); the mix averages about 15 s of work per run |
| Client | Locust 2.32.4, headless (`loadtest/locustfile.py`). Open loop ([D-036](decisions.md)): each simulated analyst submits every `PACING` seconds whatever the system does, and polls `GET /runs/{id}` every second until a final status. The measured request is *submit to final status*; the POST is reported separately |
| Ramp | Steps of simulated analysts (`STEPS=users:seconds,...`), then a drain with no new submissions until every run has finished. Each run is attributed to the step in which it was submitted |
| Evidence | Locust CSVs and per-step summaries in `loadtest/results/`; Prometheus over each window (`loadtest/prom_snapshot.py`, saved as `*_prometheus.json`); per-run timings from Postgres (`runs.result_json` traces, `llm_calls`) |

One unloaded run (S01): 18.0 s from submission to final status, of which 16.5 s in the
graph (4 model calls of 4.0 s, 14 tool calls) and 1.5 s queueing and job start.

## Results at the declared load (0.1 submissions/s, 10 minutes)

| | (a) 1 worker | (d) 3 workers |
|---|---|---|
| Runs submitted | 61 | 61 |
| Completed / escalated / failed | 42 / 19 / 0 | 42 / 19 / 0 |
| Throughput (runs finished per second) | 0.066 | 0.100 |
| **p50, submit to final status** | **184.6 s** | **17.3 s** |
| **p95, submit to final status** (target <= 60 s) | **316.8 s: not met** | **18.5 s: met** |
| Failed runs (target < 1%) | 0%: met | 0%: met |
| Submissions accepted (target >= 99%) | 61 of 61: met | 61 of 61: met |
| POST /disputes p50 / p95 | 33 / 140 ms | 37 / 85 ms |
| Worker utilisation (mean) | 98.9% | 48.7% |
| Queue depth, max | 22 | 0 |
| Processing time per run, p95 (server) | 19.9 s | 19.6 s |

With one worker every run waited about 7 s longer than the one before it: the worker
finishes about 0.066 runs/s and 0.1/s arrive, so the queue grew for the whole test
(first run 19 s, last runs about 330 s) and emptied only in the drain.

## Ramps: where the target fails

**(b) 1 worker**, `PACING=40` (0.025/s per analyst), 5 minutes per step:

| Step | Offered rate | Runs | p50 | p95 | SLO | Worker utilisation | Queue max (end) |
|------|-------------:|-----:|----:|----:|-----|-------------------:|----------------:|
| 1 | 0.025/s | 8 | 17.7 s | 19.1 s | met | 42% | 0 (0) |
| 2 | 0.050/s | 16 | 9.4 s | 18.9 s | met | 57% | 0 (0) |
| 3 | 0.075/s | 22 | 48.0 s | 69.1 s | **not met** | 97% | 5 (2) |
| 4 | 0.100/s | 31 | 134.5 s | 191.6 s | not met | 97% | 14 (12) |

**(c) 3 workers**, `PACING=20` (0.05/s per analyst), 4 minutes per step. Steps 1 to 3 come
from run `c_3workers_ramp`; steps 4 to 6 from `c2_3workers_ramp_upper` (see Caveats):

| Step | Offered rate | Runs | p50 | p95 | SLO | Worker utilisation | Queue max (end) |
|------|-------------:|-----:|----:|----:|-----|-------------------:|----------------:|
| 1 | 0.05/s | 13 | 17.6 s | 18.8 s | met | 20% | 0 (0) |
| 2 | 0.10/s | 25 | 17.4 s | 18.7 s | met | 45% | 0 (0) |
| 3 | 0.15/s | 35 | 17.3 s | 19.5 s | met | 62% | 0 (0) |
| 4 | 0.20/s | 49 | 59.7 s | 96.0 s | **not met** | 98% | 15 (12) |
| 5 | 0.25/s | 59 | 173.6 s | 344.2 s | not met | 99% | 33 (30) |
| 6 | 0.30/s | 78 | 394.7 s | 458.7 s | not met | 94% | 78 (78) |

**Failing load:** 1 worker between 0.05/s and 0.075/s; 3 workers between 0.15/s and
0.2/s. In concurrent runs (Little's law, rate x time in the system), the target fails at
about 3 to 4 runs in flight per worker; up to that, runs barely queue.

## What limits throughput

Read from Prometheus per step and from the runs' own timings in Postgres, not assumed:

- **Workers: the bottleneck.** At the failing step utilisation reaches 97 to 99% and the
  queue grows step after step. Splitting each run's time from Postgres (3-worker run,
  steps 4 to 6): the graph itself took a steady p50 of 16.3 to 16.9 s in every step, model
  time and tool time did not grow, and saving the result took under 1 s; all of the growth
  was waiting in the queue (p50 42 s, then 156 s, then 376 s). An RQ worker runs one job
  at a time and spends about 16 of its 17 s per run waiting for 4 model calls, so its
  capacity is 1 / (mean work per run), about 0.067 runs/s for this mix. Three workers
  measured about 0.2/s, three times one: workers scale linearly here.
- **Not CPU or memory.** The busiest container was the worker (0.16 cores for one worker at
  0.1/s; under 2 cores for three workers at 0.2 to 0.25/s, out of 8). The VM never used
  more than 2.6 of its 3.8 GB.
- **Not the gateway.** `llm_calls` shows 4.02 to 4.05 s per call (the stub's 4 s) in every
  minute of every test, 1 attempt each; the gateway used at most 0.4 cores.
- **Not Postgres or Redis** at these rates: at most 0.55 and 0.11 cores.
- **The API, next.** Up to 0.2/s, API p95 stayed under 0.5 s. At 0.3/s, with about 300 runs
  in flight each polled every second (29 GET/s), the API's single uvicorn process reached
  POST p95 7.8 s and GET p95 3.5 s, and 7 of 31,783 polls lost their connection. With more
  workers, the API's polling load would be the next limit: more uvicorn workers, or clients
  polling with backoff, would move it.

Sizing: to stay at or below about 70% utilisation at 0.1/s with 15 s of work per run needs
0.1 x 15 / 0.7, about 2.1 workers, so 3; measured utilisation with 3 was 49%.

**With the real model** the limit is different: at the free tier's 4 requests per minute
the gateway's rate limiter allows about one run per minute however many workers there are
(docs/agents.md, "Limits"). These tests measure this system, as the SLO asks, not that
quota.

## Caveats

- **Simulated model.** Every call takes exactly 4 s and always parses, so there are no
  third Investigator turns, no retries and no provider errors. A real model varies, and
  slower calls lower each worker's capacity in proportion.
- **Laptop.** All services, the load generator and the observability stack shared one
  MacBook Air; Docker Desktop's VM had 3.8 GB.
- **The Mac slept during two tests.** During `c_3workers_ramp` it went into idle sleep
  (19:49:32 to 19:55:50, `pmset -g log`), which stopped the VM for about 6 minutes; steps 4
  to 6 of that run are invalid and were re-run as `c2_3workers_ramp_upper` with the machine
  kept awake (`caffeinate`, now built into `make loadtest`). Steps 1 to 3 had finished
  before the sleep. During `b_1worker_ramp` the Mac slept from 12:43, after the last step
  had ended, during the drain: that can only have lengthened the last one or two queued
  runs of step 4, which fails the target by a wide margin either way.
- **Polling.** Clients poll every second, so a measured time can be up to 1 s late. The
  server-side `run_end_to_end_seconds` histogram agrees within its bucket widths; its
  buckets (20, 30, 45, 60, ... s) are too coarse for exact p95s, so this page uses the
  client measurement and the runs' own timestamps.
- **Mix per step.** The scenarios are sent in a fixed rotation, so a short step can contain
  more complete-path runs than another (step 1 of (b) was all complete-path). The p50 of
  9.4 s in (b) step 2 reflects its many 2-call escalations.
- **Steps inherit backlog.** A ramp step starts with whatever queue the previous one left,
  which is why the failing load is given as a range between a met and a failed step.
- **The 3-worker ramp's upper steps started without the lower ones' history**; the lower
  steps left no queue, so this changes nothing.

## Reproducing

```bash
python3.11 -m venv ~/.venvs/locust && ~/.venvs/locust/bin/pip install -r loadtest/requirements.txt
L=~/.venvs/locust/bin/locust

make loadtest-up WORKERS=1
LOAD_DRAIN_SECONDS=600 make loadtest LOCUST=$L NAME=a_1worker_0.1rps STEPS=1:600 PACING=10
LOAD_DRAIN_SECONDS=900 make loadtest LOCUST=$L NAME=b_1worker_ramp STEPS=1:300,2:300,3:300,4:300 PACING=40

make loadtest-up WORKERS=3
LOAD_DRAIN_SECONDS=900 make loadtest LOCUST=$L NAME=c_3workers_ramp STEPS=1:240,2:240,3:240,4:240,5:240,6:240 PACING=20
LOAD_DRAIN_SECONDS=900 make loadtest LOCUST=$L NAME=d_3workers_0.1rps STEPS=1:600 PACING=10

python3 loadtest/prom_snapshot.py --start <unix> --end <unix> --out loadtest/results/<name>_prometheus.json
docker compose down
```

Both targets refuse to run unless llm-gateway reports the stub provider. While a test runs,
the Grafana dashboard (http://localhost:3300) shows the queue, worker utilisation and the
SLO panels live.
