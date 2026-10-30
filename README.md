# Dispute Triage Engine

[![CI](https://github.com/soniaa27/Card-Dispute-Triage-Service-/actions/workflows/ci.yml/badge.svg)](https://github.com/soniaa27/Card-Dispute-Triage-Service-/actions/workflows/ci.yml)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue)
![Docker Compose](https://img.shields.io/badge/docker-compose-2496ED)

A multi-agent service that triages reports of unauthorised card and UPI transactions for an
Indian bank's disputes team.

Given a customer's dispute, the service validates the report, classifies the fraud pattern,
works out the customer's liability under the
[RBI circular of 6 July 2017](corpus/README.md) and IDBI Bank's customer protection policy,
and recommends one of four routes, each backed by clause-level citations:

| Route | Meaning |
|-------|---------|
| `chargeback` | Raise a chargeback with the card network |
| `provisional_credit` | Credit the customer provisionally while the bank investigates |
| `decline` | The policy places liability on the customer |
| `escalate` | Fault is contested, provisions conflict or the case is out of scope: an analyst must decide |

> **Decision support only.** The service never issues a credit or closes a dispute. Every
> recommendation goes to a disputes analyst, and every answer it returns has been checked
> by a second agent against the cited provisions.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [How a dispute is triaged](#how-a-dispute-is-triaged)
- [Getting started](#getting-started)
- [Usage](#usage)
- [Configuration](#configuration)
- [Development](#development)
- [Evaluation](#evaluation)
- [Project structure](#project-structure)
- [Documentation](#documentation)
- [Contributing](#contributing)

## Features

- **Strict ingestion boundary.** Requests are validated against a published JSON Schema
  plus cross-field rules. Invalid requests are quarantined with field-level errors and
  never reach the agents.
- **Grounded retrieval.** The two policy documents are chunked at clause level (tables
  included) and searched with a hybrid FAISS + BM25 retriever.
- **Two cooperating agents.** An *Investigator* drafts the position from retrieved clauses;
  a *Verifier* checks every citation, applies hard rules in code and searches for
  conflicting provisions before anything is returned. Both run in a LangGraph graph with
  shared step, tool-call and wall-clock limits.
- **Never an unverified answer.** A draft that fails verification is sent back for one
  redraft, then escalated to an analyst.
- **Resilient LLM gateway.** Timeouts, retries, a circuit breaker, rate limiting and
  per-step cost tracking. Providers are switched by configuration (`stub` or `gemini`).
- **Asynchronous and scalable.** FastAPI enqueues runs on Redis; any number of RQ workers
  process them. Every log line carries a correlation id and run id.
- **Evaluation suite.** 24 scenarios covering clear-cut cases, table boundaries,
  contested fault and out-of-scope reports, with a validator, batch runner and report diff.

## Architecture

```
client ──HTTP──▶ api (FastAPI) ──enqueue──▶ redis ──▶ worker x N (RQ) ──▶ agent graph
                  │  validate / quarantine                 │                  │
                  └──────────────▶ postgres ◀──────────────┘          llm-gateway ──▶ LLM provider
                                   runs, quarantine, feedback
```

Five services run under Docker Compose:

| Service | Role |
|---------|------|
| `api` | FastAPI: ingestion, quarantine, run status, feedback, health, metrics and the web client |
| `worker` | RQ worker (scalable): runs the agents, enforces the run timeout, records the result |
| `redis` | Job queue |
| `postgres` | Runs, quarantined requests, feedback and LLM call records |
| `llm-gateway` | Single entry point to the model provider, with resilience and cost tracking |

Deployment boundaries are described in [docs/architecture.md](docs/architecture.md).

## How a dispute is triaged

```
investigate ──▶ verify ──▶ return ────────▶ finalize
     ▲             │
     └── retry ────┤
                   └──▶ escalate ──────▶ finalize
```

1. **Investigate.** The Investigator searches the corpus, looks up clauses and drafts a
   fraud type, fault hypothesis, route and position with citations.
2. **Verify.** The Verifier:
   - checks that every citation resolves to real clause text;
   - recomputes the working-day reporting delay;
   - applies hard rules in code (for example, a delay of exactly 3 or 7 working days, an
     unclear fault, or customer statements that contradict the draft's fault);
   - asks the model which claims the cited text supports, and searches for provisions that
     could conflict.
3. **Decide.** A verified draft is returned. Unsupported claims trigger a redraft, up to
   `AGENT_MAX_VERIFY_RETRIES` times. Contested fault, conflicting provisions and
   out-of-scope cases are escalated with the relevant clauses attached.

Topology, hand-offs, limits and failure behaviour are documented in
[docs/agents.md](docs/agents.md).

## Getting started

### Prerequisites

- Docker with Compose v2 (at least 8 GB free disk for images)
- Python 3.11 and `make`, only for running tests and tools locally

### Run the stack

```bash
git clone https://github.com/soniaa27/Card-Dispute-Triage-Service-.git
cd Card-Dispute-Triage-Service-
cp .env.example .env
docker compose up --build --wait
```

The first build of the worker image also builds the corpus index: it downloads the
embedding model (about 65 MB) and embeds the corpus, which takes a few minutes. Later
builds reuse that layer until the PDFs, `ingest/` or `EMBED_MODEL` change.

When all five services report healthy:

- web client: http://localhost:8000
- interactive API docs: http://localhost:8000/docs

Check the stack end to end:

```bash
make smoke    # a malformed request is quarantined and a valid run completes
```

Stop it with `docker compose down` (add `-v` to delete the Postgres volume).

The defaults in `.env.example` use the **stub** model provider and the **mock** agent (`AGENT_MODE=fake`), so a
fresh clone runs without any API key. See [Configuration](#configuration) to enable the
real agents.

## Usage

Submit a dispute:

```bash
curl -s -X POST http://localhost:8000/disputes \
  -H 'Content-Type: application/json' \
  -d '{
    "customer_id": "CUST-100201",
    "account_type": "SAVINGS",
    "channel": "ATM",
    "transaction_id": "TXN-2026-090801",
    "transaction_amount_inr": 20000.0,
    "transaction_datetime": "2026-09-08T22:40:00+05:30",
    "bank_alert_datetime": "2026-09-08T22:41:00+05:30",
    "reported_datetime": "2026-09-09T10:15:00+05:30",
    "home_branch_state": "Karnataka",
    "complaint_text": "Rs 20,000 was withdrawn from an ATM in Pune while my card was with me.",
    "customer_claims": {"shared_otp_or_pin": false, "card_in_possession": true, "received_alert": true}
  }'
```

The response is `202` with a `run_id` and a `status_url`. Poll it until the status is
`completed`, `escalated` or `failed`:

```bash
curl -s http://localhost:8000/runs/<run_id>
```

### API reference

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/disputes` | Submit a dispute. `202 {run_id, status, status_url}` or `422 {detail, quarantine_id}` |
| GET | `/runs/{run_id}` | Status (`queued`, `running`, `completed`, `escalated`, `failed`) and the result when done |
| POST | `/runs/{run_id}/feedback` | Analyst feedback: `{satisfactory: bool, reason: str}` |
| GET | `/quarantine?limit=50` | Recently rejected requests with their errors |
| GET | `/healthz` | Liveness |
| GET | `/readyz` | Readiness (checks Redis and Postgres) |
| GET | `/metrics` | Prometheus metrics |
| GET | `/` | Web client |

Every response carries an `X-Correlation-ID` header (generated if the caller does not send
one). It is passed to the worker with the job and appears on every JSON log line.

### Data contract

The contract for `POST /disputes` is
[`contracts/schema/dispute_request.schema.json`](contracts/schema/dispute_request.schema.json),
generated from [`contracts/dispute.py`](contracts/dispute.py) with `make schema`; CI fails if
it is stale. It also appears in the OpenAPI document at `/openapi.json`.

Cross-field rules enforced on top of types, ranges and enums:

- `reported_datetime >= transaction_datetime`
- `bank_alert_datetime >= transaction_datetime`, if present
- no datetime in the future (5 minutes of clock skew tolerated); every datetime needs a
  timezone offset
- `customer_claims.card_in_possession` must be null when `channel` is `UPI`
- unknown fields are rejected

A request that fails validation is stored in the `quarantine` table and gets `422`. No run
is created and nothing is enqueued.

## Configuration

All configuration is through environment variables, loaded with pydantic-settings and
documented in [`.env.example`](.env.example). The repository contains no credentials.

### Agent mode

| `AGENT_MODE` | Behaviour |
|--------------|-----------|
| `fake` (default) | Mock agent: fixed delay and placeholder citations; for demos, CI and load tests |
| `investigator` | The Investigator agent alone |
| `graph` | Investigator and Verifier in the LangGraph graph (recommended with a real model) |

Run limits: `AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, `AGENT_MAX_WALL_CLOCK_SECONDS` and
`AGENT_MAX_VERIFY_RETRIES`. Recommended values for a rate-limited model are in
[docs/agents.md](docs/agents.md).

### LLM provider

| `LLM_PROVIDER` | Needs | Use |
|----------------|-------|-----|
| `stub` (default) | nothing | Fixed response after `STUB_DELAY_MS`; clean clones, CI, load tests |
| `gemini` | `LLM_MODEL` and `LLM_API_KEY` in your local `.env` | Google Gemini via `google-genai` |

To use Gemini, set `LLM_PROVIDER=gemini` and `LLM_API_KEY` in your local `.env` (never commit
it) and restart the gateway with `docker compose up -d llm-gateway`. The key is passed to
the `llm-gateway` container only. To force the stub for one run without editing `.env`:
`LLM_PROVIDER=stub docker compose up -d llm-gateway`.

Timeouts, retries, the circuit breaker, rate limiting and cost tracking are described in
[docs/llm_gateway.md](docs/llm_gateway.md).

## Development

Unit tests use SQLite, fakeredis and scripted model replies, so they need neither Docker
nor an API key.

```bash
make venv     # create .venv with the dev requirements (Python 3.11)
make test     # pytest
make lint     # ruff
make schema   # regenerate the JSON Schema after changing contracts/
```

Stack targets: `make up`, `make down`, `make logs`, `make ps`, `make scale WORKERS=3`,
`make smoke`, `make clean`. Run `make help` for the full list.

LLM gateway tools (stack running):

```bash
make llm-breaker        # circuit breaker state and counters
make llm-breaker-demo   # fault injection -> breaker opens -> 503 fast-fail -> recovery (stub)
make llm-cost-report    # tokens and cost per step and per run
make llm-live-test      # one real call to the provider in .env (excluded from make test and CI)
```

Corpus and retrieval:

```bash
make ingest             # corpus/raw/ -> data/chunks.jsonl + manifest (python -m ingest build)
make ingest-status      # is data/ up to date? (exit 1 if not)
make retrieval-eval     # recall@k and MRR on the scenarios
python scripts/hash_corpus.py   # SHA-256 of the source PDFs
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs ruff, the schema freshness
check, pytest and `docker compose build` on every pull request and push to `main`.

## Evaluation

The evaluation set has 24 scenarios with expected fraud type, fault, delay, route and
citations. See [docs/evaluation.md](docs/evaluation.md).

```bash
make eval-validate      # contract, working days, clause ids, verbatim passages, coverage
make eval               # run the set against the current agent; report in eval/reports/
make eval ARGS="--prompt-version v2 --ids S01,S14"
make eval-compare A=eval/reports/<a>.json B=eval/reports/<b>.json
make eval ARGS="--ids S01,S04,S14 --judge-client gateway"   # adds LLM-judged metrics
```

Running the evaluation against a real provider makes model calls; check your quota first.

## Project structure

| Path | Contents |
|------|----------|
| `contracts/` | Pydantic data contracts (source of truth) and the generated JSON Schema |
| `api/` | FastAPI app: ingestion, quarantine, runs, feedback, health, metrics |
| `worker/` | RQ worker: status transitions, timeout and failure handling |
| `agent/` | Agent runner, `investigator/`, `verifier/`, the LangGraph `graph.py` and versioned prompts in `prompts/` |
| `llm_gateway/` | `/v1/complete`: providers, retry, circuit breaker, rate limit, cost tracking |
| `ingest/` | Corpus pipeline: PDF text, clause chunking, verified tables, build manifest |
| `retrieval/` | `HybridRetriever`: dense (FAISS) and keyword (BM25) search, clause lookup |
| `eval/` | Scenarios, validator, batch runner, metrics, report comparison |
| `corpus/` | Source PDFs and their provenance |
| `common/` | JSON logging and SQLAlchemy models shared by the api and worker |
| `web/` | Static HTML and vanilla JS client |
| `observability/` | Monitoring configuration |
| `loadtest/` | Locust load test |
| `scripts/` | Smoke test, breaker demo, cost report, corpus hashing |
| `tests/` | Unit and integration tests |
| `docs/` | Architecture, agents, decisions, interfaces, evaluation, runbook, incident log |

## Documentation

- [Architecture](docs/architecture.md)
- [Agents: topology, hand-offs, limits, failure behaviour](docs/agents.md)
- [Design decisions](docs/decisions.md)
- [Agent interfaces and clause ID conventions](docs/interfaces.md)
- [Evaluation set and metrics](docs/evaluation.md)
- [LLM gateway: providers, resilience, cost](docs/llm_gateway.md)
- [Runbook](docs/runbook.md)
- [Incident log](docs/incident_log.md)
- [Corpus sources](corpus/README.md)

## Contributing

- `main` is protected: changes land through pull requests with passing CI and one approval.
- Changes to `contracts/` must include the regenerated schema in the same pull request.
- Run `make lint` and `make test` before opening a pull request.

Built as Mini Project for ML System Design and AgentOps (UE24AM342AA5), PES University.
