# Developer shortcuts. Containers run everything; the local venv is only for tests/lint.
PY ?= .venv/bin/python
WORKERS ?= 3
# Host-side URL of the gateway (published on 127.0.0.1 by docker compose), for the eval judge.
LLM_GATEWAY_HOST_PORT ?= 8100
HOST_LLM_GATEWAY_URL ?= http://localhost:$(LLM_GATEWAY_HOST_PORT)

.PHONY: help env venv up down logs ps test lint schema scale smoke clean eval eval-validate eval-compare \
	llm-live-test llm-breaker-demo llm-breaker llm-cost-report ingest ingest-status

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-14s %s\n", $$1, $$2}'

env: ## create .env from .env.example if missing
	@test -f .env || cp .env.example .env

venv: ## create local .venv with dev dependencies (Python 3.11)
	python3.11 -m venv .venv && $(PY) -m pip install -q -r requirements-dev.txt

up: env ## build and start the stack in the background
	docker compose up -d --build --wait

down: ## stop the stack (keeps the postgres volume; add -v to wipe it)
	docker compose down

logs: ## follow logs of all services
	docker compose logs -f

ps: ## show service status and health
	docker compose ps

test: ## run unit tests (SQLite + fakeredis, no Docker needed)
	$(PY) -m pytest

lint: ## ruff lint
	$(PY) -m ruff check .

schema: ## regenerate contracts/schema/dispute_request.schema.json
	$(PY) -m contracts.export_schema

scale: env ## run with $(WORKERS) workers
	docker compose up -d --build --wait --scale worker=$(WORKERS)

smoke: ## end-to-end smoke test against the running stack
	./scripts/smoke_test.sh

clean: ## stop the stack and delete its volumes
	docker compose down -v

ingest: ## parse and chunk corpus/raw/ into data/ (skips if fresh; ARGS="--force")
	$(PY) -m ingest build $(ARGS)

ingest-status: ## is data/ up to date with corpus/raw/ and the ingest code? (exit 1 if not)
	$(PY) -m ingest status

eval-validate: ## check scenarios: contract, working days, clause ids, verbatim passages, coverage
	$(PY) -m eval.validate

eval: ## run the evaluation set (ARGS="--ids S01,S14 --judge-client gateway" etc.)
	LLM_GATEWAY_URL=$(HOST_LLM_GATEWAY_URL) $(PY) -m eval.run_eval $(ARGS)

eval-compare: ## compare two reports: make eval-compare A=eval/reports/a.json B=eval/reports/b.json
	@test -n "$(A)" -a -n "$(B)" || (echo "usage: make eval-compare A=report_a.json B=report_b.json"; exit 2)
	$(PY) -m eval.compare $(A) $(B)

llm-live-test: ## one real provider call using LLM_PROVIDER/LLM_MODEL/LLM_API_KEY from .env
	$(PY) -m pytest -m live -s tests/test_llm_gateway.py

llm-breaker: ## show the gateway's circuit breaker state and counters
	@curl -fsS $(HOST_LLM_GATEWAY_URL)/v1/breaker | python3 -m json.tool

llm-breaker-demo: env ## fault injection -> breaker opens -> 503 fast-fail -> recovery (stub provider)
	./scripts/llm_breaker_demo.sh

llm-cost-report: ## tokens and cost per run and per step from llm_calls (ARGS="--since-hours 24")
	docker compose exec llm-gateway python scripts/cost_report.py $(ARGS)
