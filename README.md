# Card Dispute Triage Service

Mini Project 02, *ML System Design & AgentOps* (UE24AM342AA5).

A multi-agent service that accepts a report of an unauthorised card/UPI transaction,
validates it, classifies the fraud pattern, determines the customer's liability position from
the RBI circular (6 July 2017) and IDBI Bank's customer protection policy, and routes it to
**chargeback**, **provisional credit**, **decline** or **escalate**, with clause-level
citations. The system never issues a credit or closes a dispute; a disputes analyst decides.

> **Status: scaffold.** The pipeline runs end to end with a **fake agent**
> (`agent/runner.py`): fixed 2 s delay, placeholder citations, escalates when the complaint
> mentions "OTP". Corpus ingestion and retrieval are done (`python -m ingest build`:
> clause-level chunks with verified tables, a FAISS index, and a hybrid FAISS + BM25
> retriever; the worker image builds the index). The real agents and observability come next.

## Architecture

```
client ──HTTP──▶ api (FastAPI) ──enqueue──▶ redis ──▶ worker x N (RQ) ──▶ agent.run_agent
                  │  validate / quarantine                 │                  │
                  └──────────────▶ postgres ◀──────────────┘          llm-gateway ──▶ Gemini
                                   runs, quarantine, feedback
```

Five services under Docker Compose: `api`, `worker` (scalable), `redis`, `postgres`,
`llm-gateway`. Details and deployment boundaries: [docs/architecture.md](docs/architecture.md).
Decisions and rejected alternatives: [docs/decisions.md](docs/decisions.md).

## Quickstart (from a clean clone)

Requires Docker with Compose v2.

```bash
cp .env.example .env
docker compose up --build          # wait until all 5 services report healthy
```

The first worker build also builds the corpus index inside the image. It downloads the
embedding model (about 65 MB) and embeds the corpus, which takes a few minutes. Later
builds reuse that layer until the PDFs, `ingest/` or `EMBED_MODEL` change.

Then open http://localhost:8000 for the web client, or http://localhost:8000/docs for the API.

In another terminal:

```bash
make smoke                         # malformed request quarantined + valid run completed
docker compose up -d --scale worker=3   # or: make scale
```

Stop with `docker compose down` (add `-v` to delete the Postgres volume).

## Data contract

The published contract for `POST /disputes` is
[`contracts/schema/dispute_request.schema.json`](contracts/schema/dispute_request.schema.json),
generated from [`contracts/dispute.py`](contracts/dispute.py) with `make schema`
(CI fails if it is stale). It is also in the OpenAPI document at `/openapi.json`.

Cross-field rules enforced at the ingestion boundary (beyond schema/type/range/enum):

- `reported_datetime >= transaction_datetime`
- `bank_alert_datetime >= transaction_datetime` (if present)
- no datetime in the future (5 min skew tolerated); all datetimes need a timezone offset
- `customer_claims.card_in_possession` must be null when `channel` is `UPI`
- unknown fields are rejected

A request that fails is stored in the `quarantine` table with its field-level errors and
gets `422`. **No run is created and nothing is enqueued.**

## API

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/disputes` | Submit a dispute. `202 {run_id, status, status_url}` or `422 {detail, quarantine_id}` |
| GET | `/runs/{run_id}` | Status (`queued`, `running`, `completed`, `escalated`, `failed`) and `RunResult` when done |
| POST | `/runs/{run_id}/feedback` | `{satisfactory: bool, reason: str}`; persisted |
| GET | `/quarantine?limit=50` | Recent rejected requests with their errors |
| GET | `/healthz` | Liveness |
| GET | `/readyz` | Readiness: checks Redis and Postgres |
| GET | `/metrics` | Prometheus metrics |
| GET | `/` | Web client |

Every response carries `X-Correlation-ID` (generated if the caller does not send one). It is
passed to the worker with the job, and every JSON log line in every service carries
`correlation_id` and `run_id`.

## Configuration

All configuration is environment variables, loaded with pydantic-settings and documented
in [`.env.example`](.env.example). The repository contains no credentials or endpoints, and
the only model identifier is the default in `.env.example`.

### LLM provider

`LLM_PROVIDER` selects the gateway's provider. Switching is configuration only:

| `LLM_PROVIDER` | Needs | Use |
|----------------|-------|-----|
| `stub` (default in `.env.example`) | nothing | fixed response after `STUB_DELAY_MS`; clean clones, CI, load tests |
| `gemini` | `LLM_MODEL`, `LLM_API_KEY` in your own `.env` | Google Gemini via `google-genai` |

To use Gemini, set `LLM_PROVIDER=gemini` and `LLM_API_KEY=<your AI Studio key>` in your
local `.env` (never commit it), then `docker compose up -d llm-gateway`. docker compose passes
the key to the `llm-gateway` container only. To force the stub for one run without editing
`.env`: `LLM_PROVIDER=stub docker compose up -d llm-gateway`. Timeouts, retries, the circuit
breaker, rate limiting and cost tracking are described in
[docs/llm_gateway.md](docs/llm_gateway.md).

## Development

Unit tests use SQLite and fakeredis, so they need no Docker and run in about a second.

```bash
make venv     # python3.11 -m venv .venv + dev requirements
make test     # pytest
make lint     # ruff
make schema   # regenerate the JSON Schema after changing contracts/
```

Other targets: `make up`, `make down`, `make logs`, `make ps`, `make scale WORKERS=3`,
`make smoke`, `make clean`. Run `make help` for the list.

Evaluation (24 scenarios; see [docs/evaluation.md](docs/evaluation.md)):

```bash
make eval-validate                  # contract, working days, clause ids, verbatim passages, coverage
make eval                           # run the set against the current agent; report in eval/reports/
make eval ARGS="--prompt-version v2 --ids S01,S14"
make eval-compare A=eval/reports/<a>.json B=eval/reports/<b>.json
make eval ARGS="--ids S01,S04,S14 --judge-client gateway"   # LLM-judged metrics via the gateway
```

LLM gateway (stack running):

```bash
make llm-live-test      # one real call to the provider in .env (skipped by make test and CI)
make llm-breaker        # circuit breaker state and counters
make llm-breaker-demo   # fault injection -> breaker opens -> 503 fast-fail -> recovery (stub)
make llm-cost-report    # tokens and cost per step and per run, per 100 runs, largest driver
```

Other CLIs:

```bash
python -m ingest build              # or make ingest: corpus/raw/ -> data/chunks.jsonl + manifest
python -m ingest status             # or make ingest-status: is data/ fresh? (exit 1 if not)
python -m retrieval.evaluate        # or make retrieval-eval: recall@k / MRR on the scenarios
python scripts/hash_corpus.py       # SHA-256 of files in corpus/raw/
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs ruff, the schema
freshness check, pytest and `docker compose build` on every PR and push to `main`.

## Repository layout

| Path | Contents | Owner |
|------|----------|-------|
| `contracts/` | Pydantic data contracts (source of truth) + generated JSON Schema | shared |
| `contracts/agent.py` | Agent hand-offs: `Passage`, `InvestigatorOutput`, `VerifierOutput`, `AgentState`, `DocId` | shared |
| `contracts/llm.py` | `LLMClient` protocol, `LLMResponse`, `LLMUnavailableError` | shared |
| `contracts/tools.py` | `Retriever` protocol and `compute_working_days` | shared |
| `common/` | JSON logging and SQLAlchemy models used by api and worker | Person B |
| `api/` | FastAPI app: ingestion, quarantine, runs, feedback, health, metrics | Person B |
| `worker/` | RQ worker: status transitions, timeout, failure handling | Person B |
| `llm_gateway/` | `/v1/complete`: providers (stub, gemini), retry, breaker, rate limit, cost | Person B |
| `web/` | Static HTML + vanilla JS client | Person B |
| `agent/` | `run_agent` (fake for now), `prompts/` (versioned YAML) | Person A |
| `ingest/` | Corpus pipeline: PDF text, clause chunking, verified tables, build manifest | Person A |
| `retrieval/` | `HybridRetriever`: dense (FAISS) + keyword (BM25) search, clause lookup | Person A |
| `eval/` | 24 scenarios, validator, batch runner, metrics, report compare | Person A |
| `corpus/` | Source PDFs in `raw/`, provenance table in README | Person A |
| `observability/` | Prometheus / Grafana / Jaeger config (placeholder) | Person B |
| `loadtest/` | Locust file (stub) | Person B |
| `docs/` | Architecture, decisions, runbook, incident log | both |
| `scripts/` | Smoke test, corpus hashing | both |
| `tests/` | Unit tests | both |

## Branching workflow

- `main` is protected: no direct pushes; merge only through a PR with passing CI.
- Feature branches are named by owner: `a/<topic>` for Person A, `b/<topic>` for Person B
  (e.g. `a/chunking`, `b/circuit-breaker`).
- Every PR is reviewed and approved by the other person before merge.
- Contract changes (`contracts/`) need both people's agreement, and the regenerated
  schema must be in the same PR.

## Docs

- [Architecture](docs/architecture.md)
- [Design decisions](docs/decisions.md)
- [Agent interfaces and clause ID conventions](docs/interfaces.md)
- [Evaluation set and metrics](docs/evaluation.md)
- [LLM gateway: providers, resilience, cost](docs/llm_gateway.md)
- [Runbook](docs/runbook.md)
- [Incident log](docs/incident_log.md)
- [Corpus sources](corpus/README.md)
