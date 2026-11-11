# Observability

Traces, metrics and run reconstruction (requirements D5, A6, F8). Everything starts with
`docker compose up`; nothing needs an account or a key.

| Tool | URL | What it is for |
|------|-----|----------------|
| Grafana | http://localhost:3300 | The dashboard **Dispute triage: service, agents, containers** (home page, no login) |
| Jaeger | http://localhost:16686 | One trace per run: API, worker, graph nodes, tool and model calls, gateway |
| Prometheus | http://localhost:9090 | Raw metrics; **Status > Targets** shows what is scraped |
| `make trace RUN=<run_id>` | terminal | Everything one run did, from Postgres and its trace |

The host ports are bound to 127.0.0.1 and can be changed with `GRAFANA_HOST_PORT`,
`JAEGER_UI_HOST_PORT` and `PROMETHEUS_HOST_PORT`. `make observability-urls` prints them.

## Traces

Every service exports OpenTelemetry spans over OTLP/HTTP to Jaeger
(`OTEL_EXPORTER_OTLP_ENDPOINT`, `http://jaeger:4318` in docker compose). Jaeger keeps them in
memory: they are lost when its container restarts.

**One trace per run.** The API's `POST /disputes` span is the root. Its context (a W3C
`traceparent`) is stored in the RQ job's `meta`, and the worker starts the job's span as its
child. Inside the worker the agent runs in a thread; the job copies its context into that
thread, so the agents' spans join the same trace. The worker's model client sends the
context to llm-gateway in the HTTP headers (httpx instrumentation), so the gateway's spans
join it too.

```
POST /disputes                                   api           step=api
└─ worker.process_run                            worker        step=worker
   ├─ node.investigate                           worker        step=investigate
   │  ├─ tool.search_corpus / tool.get_clause    worker        step=search_corpus ...
   │  └─ llm.investigator                        worker        step=investigator
   │     └─ POST (httpx client span)             worker
   │        └─ POST /v1/complete                 llm-gateway   step=investigator
   │           └─ llm.gateway                    llm-gateway
   │              └─ llm.provider.<stub|gemini>  llm-gateway   one per attempt
   ├─ node.verify                                worker        step=verify
   │  ├─ tool.get_clause / tool.related / tool.search_corpus
   │  └─ llm.verifier ... (as above)
   └─ node.finalize                              worker        step=finalize
```

**Attributes.** Every span carries `run_id`, `correlation_id` and `step`
(`common/tracing.py` copies the first two from the log context each service already binds).
Beyond those:

| Span | Attributes |
|------|------------|
| `POST /disputes` | `http.*`, `run_id` once the run is created |
| `worker.process_run` | `status`, `route`, `queue_wait_s`, `processing_s`, `end_to_end_s`, `error` |
| `node.*` | `attempt`; `outcome`, `halt_reason` when the node stopped the run; on `node.verify` `verifier_outcome`, `verifier_action`, `verifier_reason`, `unsupported_claims`; on `node.finalize` `status`, `route` |
| `tool.*` | `query`, `k`, `clause`, `results` (passage ids), `found`, `latency_ms`, `tool_call` (count so far) |
| `llm.investigator`, `llm.verifier` | `prompt_version`, `model`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `latency_ms`, `outcome` (`ok` or `error`), `error` |
| `llm.gateway` | `provider`, `model`, `prompt_version`, `outcome` (as in `llm_calls`), `attempts`, `retries`, `reason`, `breaker_state`, tokens, `cost_usd`, `latency_ms` |
| `llm.provider.*` | `model`, `timeout_s`, tokens and `finish_reason`, or `error_type` and `status_code` |

Spans that fail have status ERROR. Polling (`GET /runs/...`), health checks, `/metrics`,
`/quarantine`, `/quality` and `/v1/breaker` are not traced: they would bury the runs.

**Trace id in the run.** When a run finishes, the worker appends
`{"step": "otel_trace", "trace_id": "<32 hex>"}` to the run's `RunResult.trace` list (the
contract is unchanged: trace entries are free-form dicts). `make trace` turns it into the
Jaeger link. A run marked `failed` has no result, so no entry; find it in Jaeger by searching
service `worker` with the tag `run_id=<id>`.

**Opening a trace in Jaeger.** Service `worker`, operation `worker.process_run`, then
*Find Traces*; or search by tag, e.g. `run_id=1f0c...`; or paste the trace id from
`make trace` into the search box. The dashboard's *Recent run traces* table links to them
too.

## Metrics

Prometheus scrapes every 5 s (`observability/prometheus/prometheus.yml`):

| Job | Target | Notes |
|-----|--------|-------|
| `api` | `api:8000/metrics` | HTTP, submissions, cache, guardrails, queue depth and worker states |
| `llm-gateway` | `llm-gateway:8100/metrics` | Model requests, tokens, cost, latency, breaker |
| `worker` | every replica on port 9100 | Runs, agents, corpus index freshness. Replicas are found through Docker's DNS, so `--scale worker=N` needs no change |
| `cadvisor` | `cadvisor:8080` | CPU, memory, network per container |
| `prometheus` | itself | |

### Why the worker uses multiprocess mode

RQ forks a *work horse* process for every job, and the horse exits when the job ends, so
metrics kept in its memory would be lost. prometheus_client's multiprocess mode writes them
to files in `PROMETHEUS_MULTIPROC_DIR` (a tmpfs in the container, emptied on each start),
and the worker's main process serves the sum of all files on port 9100. By default every
process gets its own files named after its pid, which would add a set of files with every
job and slow every scrape. A worker runs one job at a time, so all work horses of a
container share one identity, `job`, and add to the same files; the main process is `main`
(`worker/observability.py`, [D-030](decisions.md)). The per-run metrics are recorded when the
run ends, from the run's trace list, so they cover every agent mode without code in the
agents.

### Metric names

System (D5):

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `run_end_to_end_seconds` | histogram | `status` | `POST /disputes` (`runs.created_at`) to the final status: the [SLO](slo.md) measure, recorded by the worker |
| `run_queue_wait_seconds` | histogram | | Submission to the worker starting the run |
| `run_processing_seconds` | histogram | `status` | Worker start to final status |
| `runs_finished_total` | counter | `status`, `mode` | Runs finished (completed, escalated, failed) |
| `worker_busy_seconds_total` | counter | | Time spent on runs; `rate()` is the worker's utilisation |
| `rq_jobs` | gauge | `state` (queued, started, failed) | Read from Redis by the API at scrape time |
| `rq_workers` | gauge | `state` (busy, idle) | |
| `http_requests_total`, `http_request_duration_seconds` | counter, histogram | `method`, `path`, `status` | API requests (route template, not raw path) |
| `disputes_accepted_total`, `disputes_quarantined_total` | counter | | Submissions |
| `llm_requests_total`, `llm_latency_seconds`, `llm_tokens_total`, `llm_cost_usd_total`, `llm_retries_total`, `llm_circuit_state` | | | Gateway ([llm_gateway.md](llm_gateway.md)) |
| `process_start_time_seconds` | gauge | `job`, `instance` | Process uptime = `time() - process_start_time_seconds` |
| `container_cpu_usage_seconds_total`, `container_memory_working_set_bytes`, `container_network_*` | | `container_label_com_docker_compose_service` | cAdvisor, per container |
| `up` | gauge | `job`, `instance` | Target reachable |
| `corpus_index_fresh`, `corpus_index_built_timestamp_seconds`, `corpus_chunks` | gauge | | Worker: is the index in the image fresh (`python -m ingest status`) |
| `worker_info` | gauge | `agent_mode` | |

Agents (A6), recorded from each run's trace list:

| Metric | Type | Labels | Meaning |
|--------|------|--------|---------|
| `agent_tasks_total` | counter | `agent` (investigator, verifier), `outcome` (ok, escalated, halted, failed) | One per node execution. Success rate = `ok` / all |
| `verifier_decisions_total` | counter | `outcome`, `action` | verified/unsupported/conflicting_provisions/out_of_scope/contested_fault, return/retry/escalate |
| `agent_graph_runs_total` | counter | `outcome`, `retried` | Graph runs; retry rate = `retried="true"` / all |
| `agent_verify_retries_total` | counter | | Investigator redrafts |
| `agent_node_seconds` | histogram | `node` | Latency per node (investigate, verify, finalize) |
| `agent_steps_per_run`, `agent_tool_calls_per_run`, `agent_llm_calls_per_run` | histogram | | Per run |
| `agent_tool_calls_total`, `agent_tool_call_seconds` | counter, histogram | `tool`, `outcome` | search_corpus, get_clause, related |
| `agent_llm_calls_total`, `agent_llm_call_seconds` | counter, histogram | `agent`, `outcome` | Model calls as the agent sees them (gateway queueing and retries included) |

## Dashboard

Provisioned from `observability/grafana/dashboards/dispute-triage.json` with the Prometheus and
Jaeger data sources (`observability/grafana/provisioning/`); it is Grafana's home page and
refreshes every 10 s. Anonymous users can view it and use Explore; it is bound to 127.0.0.1.
Edits made in the UI are not saved: change the JSON instead.

| Row | Panels |
|-----|--------|
| Service level | Run p95 and p50 (submission to final status, red above 60 s), submissions per second, share of runs failed (target < 1%), share of submissions accepted (target >= 99%), targets up; run latency over time (p50, p95, p95 queue wait, p95 processing); throughput |
| API | Latency p50/p95 per route, requests by status, error rate (HTTP 5xx and runs failed) |
| Queue and workers | Queue depth (waiting, running), workers busy and idle, utilisation per worker and mean |
| Agents | Task success rate per agent, share verified, retry rate, steps and tool/model calls per run; node latency p50/p95; tasks by outcome; Verifier decisions; tool and model call latency p95; per-run counts p95 |
| LLM gateway | Latency p50/p95 per step, requests by outcome, breaker state and retries |
| Containers and uptime | CPU, memory and network per compose service (cAdvisor), process uptime, corpus index freshness and size |
| Traces | The 20 most recent run traces from Jaeger, linked |

Ratios use the dashboard's time range, so set it to the period you are looking at (a load
test, for example).

## Reconstructing a run

```bash
make trace RUN=<run_id>
```

Runs `scripts/trace_run.py` in the llm-gateway container, which reaches Postgres and Jaeger.
It prints:

- the run: status, route, prompt version, correlation id, times, request summary,
  escalation reason, citations;
- every step in order from the run's trace list: each node with its attempt, outcome and
  latency; each tool call with its query and the passage ids it returned (or the clause it
  looked up); each model call with the agent, prompt version, model, tokens, cost and
  latency; the Investigator's draft; the Verifier's outcome, action, reason, unsupported
  claims and modifying provisions; the graph summary;
- latency and cost per step (model calls per agent, tool calls per tool, nodes);
- the gateway's own record of each model call from `llm_calls` (outcome, attempts, latency,
  tokens, cost, provider and model);
- the trace id, its Jaeger link, and how many spans Jaeger holds per service.

## Container resources (cAdvisor) on Docker Desktop

cAdvisor reads the Docker host's cgroups. On Docker Desktop for Mac the host is the Linux VM,
and cAdvisor's defaults saw only the VM as a whole (`id="/"`). It works with four changes in
`docker-compose.yml` ([D-033](decisions.md)):

- `cgroup: host`: with cgroup v2 each container has a private cgroup namespace, so cAdvisor
  otherwise sees only itself;
- the Docker socket mounted by itself (`/var/run/docker.sock`): a bind of all of `/var/run`
  does not include it on Docker Desktop;
- the containerd socket (`/run/containerd/containerd.sock`): Docker Desktop keeps images in
  containerd's store, and cAdvisor v0.55 reads container details through it (v0.52 looked
  for Docker's older layer database and failed);
- labels: `--whitelisted_container_labels=com.docker.compose.service,...` gives each series
  `container_label_com_docker_compose_service`, which the dashboard groups by.

cAdvisor sees every container on the Docker host, including other projects; the dashboard's
*Compose project* variable keeps this stack's. cAdvisor itself uses about 160 MiB.

On Linux the same configuration works; if the containerd socket path differs there, remove
that mount and cAdvisor uses Docker's API alone.

## Limits

- Jaeger stores traces in memory: a restart loses them, and the oldest go first when it is
  full. Prometheus keeps 7 days (`PROMETHEUS_RETENTION`) in the `prometheus-data` volume.
- Run metrics are recorded by the worker when a run ends. A work horse killed by RQ's hard
  timeout records nothing; `rq_jobs{state="failed"}` and the run's `failed` status show it.
- `rq_jobs` and `rq_workers` come from the API, so they are missing while the API is down.
