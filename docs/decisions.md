# Design decisions

Short records of decisions and what was rejected. Add new entries at the bottom.

## D-001 Task runner: RQ on Redis (rejected: Celery, FastAPI BackgroundTasks)

- **Chosen:** RQ. One job type (`worker.tasks.process_run`), one queue, Redis already needed.
  RQ is a few hundred lines of concepts: enqueue by function path, a worker process, a
  failed-job registry. Each job runs in a forked work-horse process, so a crashed or stuck
  run cannot corrupt the worker's parent process.
- **Rejected: Celery.** More moving parts (broker/result backend config, prefetch, acks
  semantics, beat) for features we do not need (chords, routing, scheduling). Worth
  revisiting only if we need multiple queues with priorities or rate-limited task types.
- **Rejected: FastAPI BackgroundTasks.** Runs inside the API process: no queue, no
  independent scaling, and runs are lost on API restart. Requirement A4 asks for a queue
  between submission and execution.

## D-002 PostgreSQL for runs, quarantine and feedback (rejected: Redis-only, SQLite)

- Runs, quarantined requests and feedback are audit records ("defensible to the RBI
  Ombudsman"): they need durability, ad-hoc querying (`/quarantine`, feedback aggregates,
  per-run reconstruction) and concurrent writers (API + N workers).
- **Rejected: storing results only in RQ/Redis.** Results expire with `result_ttl`, and Redis
  is configured as a cache/queue, not a system of record.
- **Rejected: SQLite in production.** Single writer; does not work across containers.
- Schema is created with `create_all` by the API only (workers wait on the API's
  healthcheck), avoiding concurrent DDL. Move to Alembic once the schema stabilises.

## D-003 Async run delivery: polling (rejected for now: callbacks/webhooks)

- `POST /disputes` returns `202 {run_id, status_url}`; the client polls `GET /runs/{run_id}`.
- **Why polling:** the only clients are our web page, the smoke test, the eval CLI and Locust,
  none of which can expose an HTTP endpoint for callbacks. Polling is stateless on the
  server, trivially retryable and survives client restarts. Runs take tens of seconds, so a
  1 s poll interval costs little.
- **Rejected for now: callbacks.** Need callback URL validation (SSRF risk), signing, retries
  and a delivery log. Can be added later as an optional `callback_url` without changing
  the polling contract.

## D-004 Wall-clock timeout in the worker, RQ timeout as backstop

- `process_run` runs the agent in a thread and waits at most `RUN_TIMEOUT_SECONDS`; on expiry
  the run is marked `failed` with an explicit error. RQ's `job_timeout` is set to
  `RUN_TIMEOUT_SECONDS + 30` as a hard kill backstop, and the worker's exception handler
  marks the run `failed` if that fires.
- A run that did not complete is never stored as an answer: only `completed`/`escalated`
  results from the agent are persisted as results; everything else becomes `failed`.

## D-005 Data contract details

- `DisputeRequest` is the source of truth (`contracts/dispute.py`); the JSON Schema in
  `contracts/schema/` is generated from it (`make schema`) and CI fails if it is stale.
- `extra="forbid"`: unknown fields are rejected, so a client cannot smuggle in fields such as
  `approve_refund`.
- Datetimes must carry a timezone offset. Reporting delay is the core input to the
  liability table; naive datetimes would make "working days elapsed" ambiguous.
- "No datetime in the future" tolerates 5 minutes of clock skew.
- Pydantic evaluates cross-field rules only after all individual fields are valid, so a
  request with both kinds of error reports the field errors first.
- The API parses the body itself (not via a typed FastAPI parameter) so that the exact raw
  payload, even non-JSON, can be quarantined with its errors.

## D-006 Tests use SQLite + fakeredis

- Unit tests run without Docker in under a second: the API and worker take their engine and
  Redis connection by injection. The real Postgres/Redis path is covered by `make smoke`
  against the compose stack. Trade-off: Postgres-specific behaviour is not unit-tested.

## D-007 Shared `common/` package

- `common/` holds logging setup and the SQLAlchemy models used by both api and worker.
  Each image copies only what it needs (the gateway copies only `common/logging.py`).

## D-008 Retriever and LLM client are `typing.Protocol`s (rejected: ABCs, concrete classes)

- `contracts/tools.py: Retriever` and `contracts/llm.py: LLMClient` define only method
  signatures. Agents type against them and receive an instance, so:
  - Person 1 can build the FAISS/Chroma retriever and Person 2 the Verifier at the same
    time, against the same signatures.
  - The provider can be changed by configuration (the brief's section 12) without
    touching agent code.
  - Tests can pass any object with the right methods; no inheritance needed.
- **Rejected: abstract base classes.** They force implementations to import and subclass
  our package and add nothing that the type checker and `runtime_checkable` don't give.
- **Rejected: calling LangChain/LlamaIndex or a provider SDK directly from the agents.**
  That ties agent code to one library and makes the circuit breaker and cost accounting
  impossible to enforce in one place.

## D-009 Working days are computed in code, not by the LLM

- The reporting delay picks the row of the liability table (within 3 / 4 to 7 / beyond 7
  working days), so an off-by-one changes the outcome. It must be exact, reproducible and
  testable, which a language model is not. `contracts.tools.compute_working_days` is
  deterministic and unit-tested. Both agents call it; the Verifier recomputes it rather
  than trusting the Investigator's number.
- **Counting rule:** days after the communication date up to and including the reporting
  date. Sundays and 2nd/4th Saturdays are non-working, plus holidays passed in.
- **Known conflict in the corpus.** The RBI circular's note under Table 2 excludes "the date
  of receiving the communication". IDBI policy 5.13 says the count is made "without
  considering the date of reporting". We follow the RBI note, because the circular is the
  governing regulation and the IDBI tables count "from the date of receipt of
  communication". The difference matters only at the 3- and 7-day boundaries. When the
  delay lands exactly on a boundary, the Verifier should treat it as
  `conflicting_provisions` and escalate.
- **Not yet done:** state bank-holiday calendars by `home_branch_state`. Until then,
  holidays must be passed in explicitly.
