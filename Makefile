# Developer shortcuts. Containers run everything; the local venv is only for tests/lint.
PY ?= .venv/bin/python
WORKERS ?= 3

.PHONY: help env venv up down logs ps test lint schema scale smoke clean

help:
	@grep -E '^[a-z]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-8s %s\n", $$1, $$2}'

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
