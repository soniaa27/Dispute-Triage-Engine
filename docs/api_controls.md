# API cache and rate limit: measured effect (D7)

What the duplicate-dispute cache and the per-client rate limit (`api/controls.py`,
[decisions.md D-026](decisions.md)) change for latency and model cost, measured on the
Compose stack with `scripts/measure_api_controls.py` on 11 Nov 2026.

## Setup

- One worker, `AGENT_MODE=investigator`, `LLM_PROVIDER=stub` (`STUB_DELAY_MS=1500`),
  `LLM_MAX_REQUESTS_PER_MINUTE=600` so the gateway's own limiter is not the bottleneck.
  With the stub, every run makes exactly one model call and escalates (the stub's reply is
  not a draft), about 1.7 s per run, so one worker finishes about 30 runs a minute.
- Cost uses illustrative prices, `LLM_PRICE_PER_1K_PROMPT_TOKENS=0.0003` and
  `LLM_PRICE_PER_1K_COMPLETION_TOKENS=0.0025` (the free tier is $0); the token and call
  counts are what the gateway recorded in `llm_calls`.
- Latency is end to end: POST /disputes until GET /runs/{id} shows a final status.
- The controls are switched with environment variables when starting the api:
  `CACHE_TTL_SECONDS=0` (cache off), `RATE_LIMIT_PER_MINUTE=0` (limit off).

```bash
LLM_PROVIDER=stub AGENT_MODE=investigator LLM_MAX_REQUESTS_PER_MINUTE=600 \
  docker compose up -d --build --wait postgres redis api llm-gateway worker
python scripts/measure_api_controls.py cache                         # cache on
CACHE_TTL_SECONDS=0 docker compose up -d --wait api                   # (keep the vars above)
python scripts/measure_api_controls.py cache
RATE_LIMIT_PER_MINUTE=10 RATE_LIMIT_TRUST_CLIENT_ID=true docker compose up -d --wait api
python scripts/measure_api_controls.py noisy --seconds 90
RATE_LIMIT_PER_MINUTE=0 docker compose up -d --wait api
python scripts/measure_api_controls.py noisy --seconds 30
```

Percentiles are nearest-rank. (A first run of the 10/min arm, before a rounding fix in the
script, gave p50 1.9 s and p95 17.9 s; the numbers below are the re-run.)

## Duplicate cache

40 submissions drawn with a fixed seed from 10 disputes (9 distinct ones were drawn), from 4
concurrent clients: a client that retries or resubmits, which is what the cache is for.

| | Runs started | Model calls | Tokens | Cost (USD) | p50 | p95 | Mean |
|---|---:|---:|---:|---:|---:|---:|---:|
| Cache off | 40 | 40 | 65,760 | 0.02114 | 7,172 ms | 7,263 ms | 6,925 ms |
| Cache on | 9 | 9 | 14,796 | 0.00476 | 67 ms | 7,141 ms | 1,512 ms |
| Change | -78% | -78% | -78% | **-77.5%** | -99% | **-1.7%** | -78% |

- **Cost** falls in proportion to the repeated share: 31 of 40 submissions were cache hits
  and started no run. A real graph run makes about 4 model calls (docs/agents.md), so each
  hit saves about 4 calls there, not 1.
- **p95 barely moves**, and that is expected: 9 of 40 submissions (22%) were first
  submissions, which run the agent like before, and with more than 5% misses the 95th
  percentile is a miss. The cache helps the median and the mean (hits answer in about 70 ms),
  and it shortens the queue for everyone else, but it does not make a first submission
  faster. p95 only falls when fewer than 5% of submissions are new.
- Not reused, by design: failed runs and "model unavailable" / "investigation limit
  reached" escalations (tested in `tests/test_api_controls.py`).

## Per-client rate limit

One client ("noisy") floods POST /disputes from one thread as fast as it can; a second,
well-behaved client submits one dispute every 7 s and waits for each result. Both run on one
machine, so they are told apart by `X-Client-ID` (`RATE_LIMIT_TRUST_CLIENT_ID=true`). With the
default (`false`) the limit is per IP and a rotating header does not escape it (checked live:
6 requests with 6 different ids at a limit of 3 -> 202, 202, 202, 429, 429, 429).

| Limit | Duration | Flood accepted | Flood refused (429) | Well-behaved p50 | Well-behaved p95 |
|---|---:|---:|---:|---:|---:|
| Off | 30 s | 3,231 | 0 | > 180 s (timed out) | > 180 s (timed out) |
| 10 per minute | 90 s | 30 | 4,623 | 1.9 s | 15.1 s |
| 30 per minute (victim every 2 s) | 60 s | 60 | 5,611 | 41.2 s | 55.5 s |

- **Without a limit** one client queued 3,231 runs in 30 s, more than an hour of work for one
  worker, and the other client's runs did not finish within the 3-minute cap. (The leftover
  jobs were deleted after the measurement.)
- **At 10 per minute** the flood got 30 runs in 90 s (three windows' worth) and the other
  client's median stayed at 1.9 s, the same as an idle system. Its p95 of 15.1 s is the
  fixed window's weak spot: at the start of each minute the flooder gets its 10 requests in
  at once, and a request that lands just behind them waits about 10 x 1.7 s. A sliding
  window or token bucket would spread that burst; at this scale we accept it (D-026).
- **At 30 per minute** the limit was above what one worker finishes (about 30 runs a minute),
  so a single client at the limit still filled the queue and the other client waited 41 s.
  This run used a 2 s victim interval, so it is not directly comparable to the other two
  rows; it is here because it changed the default.

**Decision:** the default is `RATE_LIMIT_PER_MINUTE=10`. A per-client limit protects other
clients only when it is below what the workers can process: set it to at most (worker
capacity per minute) / (clients expected at once). With the real model a run takes about a
minute at the free tier's 4 calls a minute, so on Gemini the limit stops floods and runaway
scripts, but fair sharing between clients would need per-client queues. A person using the
web client never comes close to 10 submissions a minute.
