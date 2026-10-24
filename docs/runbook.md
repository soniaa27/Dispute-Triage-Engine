# Runbook

Two most likely failure modes. Fill in from real experience as the system grows.

Useful commands:

```bash
docker compose ps                                     # health of every service
docker compose logs -f api worker                     # JSON logs
docker compose logs worker | grep <run_id>            # everything for one run
curl -s localhost:8000/readyz                         # api dependencies
docker compose exec redis redis-cli llen rq:queue:disputes   # queue depth
```

## 1. Runs stuck in `queued`

- **Symptom:** `GET /runs/{id}` stays `queued`; queue depth grows.
- **Confirmation:** TODO (e.g. `docker compose ps worker` shows no healthy workers; queue
  length > 0 and not decreasing; worker logs show no `run_started`).
- **Recovery:** TODO (e.g. `docker compose up -d --scale worker=N`; check `QUEUE_NAME`
  matches in api and worker; check Redis connectivity).

## 2. Runs ending `failed` (timeouts / provider errors)

- **Symptom:** many runs end with `status=failed`; `error` mentions `RUN_TIMEOUT_SECONDS`
  or a provider error.
- **Confirmation:** TODO (e.g. worker logs `run_failed` with the error class; llm-gateway
  metrics show error outcomes or high latency).
- **Recovery:** TODO (e.g. switch `LLM_MODE=stub` to isolate; raise `RUN_TIMEOUT_SECONDS`
  only with justification; wait for the circuit breaker to close).
