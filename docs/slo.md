# Service level objective

Declared on 11 November 2026, before any load testing of complete runs.

## Target
- **Latency:** p95 time for a complete run of at most 60 seconds, measured
  from POST /disputes to a final status (completed, escalated or failed)
  visible via GET /runs/{run_id}.
- **Load:** a sustained arrival rate of 0.1 submissions per second
  (one every 10 seconds).
- **Errors:** fewer than 1% of runs end as failed.
- **Availability:** at least 99% of submissions accepted (HTTP 202, or 422
  for invalid input), none lost.

## Test conditions
- The LLM is replaced by the stub provider: correctly formatted replies for
  each agent call after a fixed delay of 4 seconds per call, so the test
  measures our system, not the provider's limits.
- A typical run makes about 4 model calls (2 Investigator, up to 2 Verifier).
- The stub is exempt from the free-tier rate limiter during the test.
- Measured from submission to final result, not the submission call alone.
- The worker count is a test variable: 1 worker, then 3.

## Why this target
- The output is a recommendation for a disputes analyst, not an instant
  customer-facing answer. The regulation works in days: shadow credit within
  10 working days and resolution within 90 days (IDBI policy, clause 7).
  The target is therefore set by analyst workflow, not by the regulator.
- An analyst who submits a case should see the recommendation within about a
  minute, while the case is still in hand. Runs are asynchronous, so a longer
  wait never blocks the analyst.
- About 4 model calls of about 4 seconds, plus retrieval and overhead, gives
  roughly 20 seconds per run without queueing; 60 seconds leaves room for
  queueing at the target load.
- 0.1 submissions per second is about 8,640 per day if sustained, an assumed
  peak for one bank's disputes desk and well above average volume.
  (Assumption, to revisit.)

## What is reported
RPS, p50 and p95 against this target and whether it was met; the concurrency
at which the target fails and the component responsible; the effect of
scaling from 1 to 3 workers.

Note: the p50/p95 figures in docs/api_controls.md measure the API
submission path for caching and rate limiting, not complete runs, and are not
the basis of this target.
