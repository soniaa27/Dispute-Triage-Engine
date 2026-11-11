# Observability configuration

Configuration only; how it all works is in [docs/observability.md](../docs/observability.md).

- `prometheus/prometheus.yml`: scrape targets (api, llm-gateway, every worker replica, cAdvisor).
- `grafana/provisioning/`: the Prometheus and Jaeger data sources and the dashboard provider,
  loaded by Grafana at startup.
- `grafana/dashboards/dispute-triage.json`: the dashboard, provisioned as code. Edits in the
  Grafana UI are not saved; change this file and restart Grafana
  (`docker compose restart grafana`).

Tracing code lives with the services: `common/tracing.py`, `api/telemetry.py`,
`worker/observability.py`, `llm_gateway/telemetry.py` and `agent/tracking.py`.
