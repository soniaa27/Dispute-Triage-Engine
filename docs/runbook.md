# Runbook

Two most likely failure modes. Fill in from real experience as the system grows.

Useful commands:

```bash
docker compose ps                                     # health of every service
docker compose logs -f api worker                     # JSON logs
docker compose logs worker | grep <run_id>            # everything for one run
curl -s localhost:8000/readyz                         # api dependencies
docker compose exec redis redis-cli llen rq:queue:disputes   # queue depth
make llm-breaker                                      # gateway breaker state and counters
make llm-cost-report                                  # llm_calls by step, run and outcome
```

## 1. LLM provider unavailable or rate limited

**Symptom**

- Runs end `escalated` with reason "model unavailable" (once the agents are built), or
  `failed` on `RUN_TIMEOUT_SECONDS`.
- `HttpLLMClient` raises `LLMUnavailableError`; the gateway answers 503
  `{"error": "llm_unavailable", "reason": ...}`.
- The eval report shows judged metrics as `not computed (judge failed: ...)`.

**Confirmation**

1. **Breaker:** `make llm-breaker` (or `curl -s localhost:8100/v1/breaker`).
   - `state: open` with `trial_allowed_in_seconds` means the provider failed
     `LLM_CB_FAILURE_THRESHOLD` times in a row.
   - A rising `fast_failures` count is requests being refused without a provider call.
   - Check `fault_injection` is `off`.
2. **Metrics** (`curl -s localhost:8100/metrics`):
   - `llm_circuit_state` is 2 when open.
   - `llm_requests_total{outcome="failed"|"breaker_open"|"rate_limited"}` rising.
   - `llm_retries_total` rising.
   - The `llm_latency_seconds` tail moving towards `LLM_REQUEST_DEADLINE_SECONDS`.
3. **Logs:** `docker compose logs llm-gateway | grep -E 'provider_call_failed|breaker_fast_fail|rate_limited|FAULT'`.
   `provider_call_failed` carries `error_type` and `status_code`. For one run,
   `grep <run_id>`: every line has `run_id`, `step` and `correlation_id`.
4. **llm_calls outcomes:** `make llm-cost-report` prints a per-outcome table. Then look at
   which `error_type` dominates:
   ```bash
   docker compose exec postgres psql -U triage -d triage -c \
     "select outcome, error_type, count(*) from llm_calls
      where created_at > now() - interval '1 hour' group by 1, 2 order by 3 desc"
   ```

**What the cause looks like**

| `error_type` | Cause |
|--------------|-------|
| `rate_limited_by_provider` (429) | Provider quota. Per minute: `LLM_MAX_REQUESTS_PER_MINUTE` is set above the project's real limit. Per day: the free-tier daily quota is spent, and 429s persist until midnight Pacific time |
| `rate_limited` (gateway, no attempt) | Our own limiter. More concurrent work than `LLM_MAX_REQUESTS_PER_MINUTE` allows within `LLM_RATE_LIMIT_MAX_WAIT_SECONDS` |
| `server_error`, `timeout`, `connection` | Provider outage or network. Check the provider's status page |
| `auth` | Key revoked or wrong. Every call fails fast after the threshold; retries do not help |

**Fallback path (by design)**

When the model is unavailable, the agent must **escalate the case to a human with reason
"model unavailable"**. It must never return a confident route without the model; see
[interfaces.md](interfaces.md#when-the-model-is-unavailable). Customers are not left with a
wrong answer; the cost is human workload.

**Recovery**

- **Transient outage:** nothing to do. After `LLM_CB_RESET_SECONDS` one trial call goes
  through; if it succeeds the breaker closes (`make llm-breaker`). Escalated runs can be
  resubmitted.
- **Per-minute 429s:** lower `LLM_MAX_REQUESTS_PER_MINUTE` in `.env`, then
  `docker compose up -d llm-gateway`. The breaker state survives the restart.
- **Daily quota exhausted:** wait for the reset, or switch model or provider in `.env`
  (`LLM_MODEL`, `LLM_PROVIDER`) and restart the gateway.
- **Auth:** replace `LLM_API_KEY` in `.env` (never in the repo) and restart the gateway.
- **Load test or isolation:** `LLM_PROVIDER=stub docker compose up -d llm-gateway` takes the
  provider out of the picture.
- **Force-close the breaker** only once the cause is fixed:
  `docker compose exec redis redis-cli del llm:breaker llm:breaker:trial`.
- **Log afterwards:** record the incident in `docs/incident_log.md` with the `llm_calls`
  counts.

## 2. Runs stuck in `queued`

- **Symptom:** `GET /runs/{id}` stays `queued`; queue depth grows.
- **Confirmation:** TODO (e.g. `docker compose ps worker` shows no healthy workers; queue
  length > 0 and not decreasing; worker logs show no `run_started`).
- **Recovery:** TODO (e.g. `docker compose up -d --scale worker=N`; check `QUEUE_NAME`
  matches in api and worker; check Redis connectivity).
