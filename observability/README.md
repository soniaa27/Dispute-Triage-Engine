# Observability

Placeholder. Planned (requirements D5, A6, F8):

- `prometheus/prometheus.yml`: scrape `api:8000/metrics`, `llm-gateway:8100/metrics`, worker metrics.
- `grafana/`: provisioned datasources and a dashboard showing p50/p95 latency, error rate,
  throughput, availability, per-container resource use, and per-agent success, steps, tool
  calls and retry rate.
- Jaeger (or Tempo) receiving OpenTelemetry traces from api, worker and llm-gateway.

What exists now:

- JSON logs on stdout from every Python service, each line carrying `service`,
  `correlation_id` and `run_id` (`docker compose logs -f api worker`).
- `X-Correlation-ID` accepted and returned by the API, and passed to the worker with each job.
- `/metrics` on api and llm-gateway (prometheus_client).
