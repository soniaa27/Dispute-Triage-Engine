# Architecture

```
                 ┌──────────────── docker compose network ─────────────────────────┐
  browser /      │                                                                  │
  curl / Locust  │  ┌───────────┐  enqueue(run_id,     ┌─────────┐                  │
 ───HTTP:8000────┼─▶│    api    │──correlation_id)────▶│  redis  │                  │
                 │  │ FastAPI   │                      │ (queue) │                  │
                 │  │ + web/    │                      └────┬────┘                  │
                 │  └─────┬─────┘                           │ dequeue               │
                 │        │ runs, quarantine,               ▼                       │
                 │        │ feedback            ┌──────────────────────┐            │
                 │        ▼                     │ worker  (x N, scaled) │            │
                 │  ┌───────────┐  read request │ RQ  ->  agent.run_agent│           │
                 │  │ postgres  │◀──────────────│         (FAKE for now) │           │
                 │  │ (volume)  │  write result └──────────┬───────────┘            │
                 │  └───────────┘                          │ /v1/complete (later)    │
                 │                                         ▼                         │
                 │                                 ┌──────────────┐                  │
                 │                                 │ llm-gateway  │──▶ provider     │
                 │                                 │ stub|provider│    (TODO)       │
                 │                                 └──────────────┘                  │
                 └──────────────────────────────────────────────────────────────────┘
```

## Request lifecycle

1. `POST /disputes`: body validated against `DisputeRequest`.
   - invalid: raw body + field-level errors written to `quarantine`; `422`; nothing enqueued.
   - valid: `runs` row (`queued`), job enqueued with `run_id` and `correlation_id`; `202`.
2. A worker dequeues, sets `running`, loads the request from Postgres, calls
   `agent.run_agent` under `RUN_TIMEOUT_SECONDS`.
3. Result stored; status becomes `completed` or `escalated`; any error becomes `failed`.
4. Client polls `GET /runs/{run_id}` until a terminal status.

## Deployment boundaries

| Service | Why it is its own container |
|---------|-----------------------------|
| api | Latency-sensitive, light CPU; scales on request rate; released independently of agent logic. |
| worker | Long-running, CPU/memory heavy once embeddings and agents land; scaled horizontally (`--scale worker=N`); a crash loses at most one run, not the API. |
| llm-gateway | Single choke point for provider keys, rate limits, circuit breaker and cost accounting; swapped to stub for load tests without touching the agent. |
| postgres | Stateful system of record with its own volume and lifecycle. |
| redis | Queue; stateless enough to restart, separate failure domain from the database. |

## Failure behaviour (current)

- Worker dies mid-run: RQ moves the job to the failed registry after the work horse is
  lost. The run row may stay in `running` (see runbook; a reaper is a TODO).
- Agent raises or times out: run marked `failed` with the error; no result returned.
- Redis down at submission: run marked `failed`, API returns `503`.
