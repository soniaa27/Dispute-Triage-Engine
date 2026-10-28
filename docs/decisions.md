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

## D-010 LLM-as-judge on our LLMClient instead of TruLens

- L3 allows "TruLens or equivalent". `trulens-core` + `trulens-feedback` resolve, but pull in
  about 40 packages (pandas, scipy, scikit-learn, nltk, numpy, alembic, OpenTelemetry SDK)
  into the dev environment.
- More importantly, TruLens feedback providers call provider SDKs directly. Judge calls would
  bypass `llm-gateway`, so they would escape the per-call cost accounting (L5), the circuit
  breaker (A5) and configuration-only provider switching (section 12 of the brief).
- **Chosen:** `eval/metrics.py: LLMJudge`, about 100 lines, judging through
  `contracts.llm.LLMClient`. The metric definitions follow TruLens' RAG triad (groundedness,
  context relevance) plus faithfulness and hallucination rate, and are written out in
  `docs/evaluation.md`.
- Revisit if we need TruLens' dashboard or its pre-built feedback functions.

## D-011 Evaluation set judgement calls

Where the corpus leaves the expected outcome open, the evaluation set records these choices
(details and limitations in `docs/evaluation.md`).

- **Section 4.1 case (S14): escalate.** IDBI 5.12 lists a "Vishing Attack" as negligence, but
  the brief requires escalation and the RBI circular does not settle it.
- **Beyond 7 working days up to 60 calendar days (S07): decline**, because there is no shadow
  credit. The basis must state the IDBI Table-I row 10 compensation (up to ₹2,500 or the
  transaction value, whichever is lower); `basis_must_mention` checks it.
- **Duplicate charge (S12): chargeback.** The second debit is unauthorised (IDBI 5.21).
- **Merchant non-delivery (S13): escalate, not chargeback.** The customer authorised the
  payment, so it is outside the corpus, and there is no clause to ground a chargeback.
- **UPI PIN (S10)** is treated as the "PIN" in IDBI 5.12's list of credentials.
- **Liability value `undetermined`** marks escalations the corpus cannot settle (conflicts,
  boundaries), as distinct from `not_applicable` (out of scope).
- **Not modelled:** state bank holidays and the RBI paragraph 9 discretionary waiver.

## D-012 Gemini through google-genai, behind a provider abstraction

- **SDK:** the official `google-genai` SDK (not the deprecated `google-generativeai`, not raw
  REST). It reports token usage per response, including thinking tokens, and raises typed
  `APIError`s with the HTTP code, which map cleanly onto retryable and non-retryable errors.
- **Pinned to 2.8.0:** later releases require pydantic ≥ 2.12.5, while every service pins
  pydantic 2.10.4. Bumping pydantic across the contracts was out of scope for this branch.
- **SDK retries off:** the SDK's own retries are disabled (`HttpRetryOptions(attempts=1)`),
  so the gateway's retry count, breaker and rate limiter see every real request.
- **Provider abstraction:** `llm_gateway/providers/` holds a `Provider` protocol, one file per
  provider and a registry keyed by `LLM_PROVIDER`. Switching provider is configuration only;
  adding one is one file plus one registry entry. Agents never see a provider SDK; they use
  `LLMClient` (D-008).
- **Stub kept:** the stub stays as a provider, because load tests need a model-free gateway.

## D-013 Breaker and rate-limiter state in Redis, not in process memory

- **In memory would fail twice:**
  - Restarting the gateway would reset an open breaker, so a restart during a provider outage
    would immediately send traffic to the failing provider again.
  - With more than one gateway replica, each would have its own breaker and its own request
    budget, so N replicas would send N times the configured rate to a free-tier quota.
- **Chosen:** Redis, already in the stack for RQ.
  - The breaker is a hash plus a `SET NX EX` trial lock, so exactly one caller makes the
    half-open trial.
  - The limiter is a token bucket in one Lua script, so it is atomic across replicas.
- **Cost:** a few sub-millisecond Redis round trips per call, which is negligible next to an
  LLM call. If Redis is down the gateway cannot serve. The API and worker cannot either, so
  this adds no new failure mode.
- **Not distributed-perfect:** breaker transitions are not one transaction. Two replicas
  failing at the same moment may both record the opening; the outcome (open) is the same.

## D-014 Cost computed at call time

- **How:** the gateway computes `cost_usd` from the provider's reported token counts and the
  configured prices (`LLM_PRICE_PER_1K_*`) when the call completes. It stores the cost in
  `llm_calls` and returns it in `LLMResponse`.
- **Why at call time:**
  - Prices change. A row records what the call cost under the price in force then, so
    old reports stay correct after a price change.
  - Retrospective computation would need the price history.
  - Per-run cost is available immediately to the agent, the eval report and the metrics,
    with no batch job.
- **Tokens are stored too,** so cost can be recomputed under a different price for
  what-if analysis. On the free tier the prices are 0; tokens are the cost measure, and the
  cost report falls back to tokens to find the largest driver.

## D-015 PDF text: pypdf layout mode, checked against plain mode per page (rejected: one mode, PyMuPDF/pdfplumber)

- **Problem:** each pypdf mode fails on part of the corpus.
  - Plain mode splits many words ("wor king", "h as"), mostly in IDBI_POLICY.
  - Layout mode keeps words and table columns but garbles RBI_2017_CIRCULAR page 4
    (Table 1) into CJK characters.
- **Chosen:** extract every page in layout mode and use it only if, ignoring whitespace, it
  has the same characters as plain mode (layout may reorder text; corruption changes it).
  Otherwise fall back to plain. Then rejoin split words on every page, using word counts
  from the corpus itself (joined word must be more frequent than a fragment; a capitalised
  right part never joins). Only RBI page 4 falls back.
- **Invariant:** only whitespace changes, so chunk text stays verbatim under the
  whitespace-insensitive comparison in `eval/corpus.py`. Tested on every page.
- **Rejected: another PDF library.** `eval/corpus.py` defines "verbatim" through pypdf;
  using the same library keeps the chunker and the evaluation on the same text.

## D-016 Clause-level chunking by rules, tables as verified specs (rejected: fixed-size chunks, LLM parsing)

- **Chosen:** one chunk per clause, using each document's own numbering
  (`docs/interfaces.md`). A small rule-based splitter is configured per document
  (`ingest/documents.py`): numbering levels, headings, page furniture, fixed-id markers
  (`Table-2-note`, `Table-I-note`, `7(ii)-beyond-7`). A number opens a clause only if it
  follows the previous sibling, which also tells the letter `(i)` from the roman `(i)`.
- **Tables are transcribed, not parsed:** pypdf cannot recover merged cells (IDBI Table-I
  rows 03-07 share one liability cell) or the garbled RBI Table 1. Each table is written out
  as a spec and every cell is checked verbatim against the PDF on every build, so a typo or
  a new PDF version fails the build instead of producing a wrong citation. Each row is also
  its own chunk, labelled by column, with its reporting-delay band and footnote.
- **Rejected: fixed-size or recursive character chunks.** A chunk would span clauses, so a
  citation could not name one `clause_id`, and `get_clause` could not return exactly the
  clause the Verifier asks for.
- **Rejected: an LLM to parse the PDFs.** Not deterministic, costs calls on a 4 RPM free
  tier, and its output would still need the same verbatim check.
- **Cost:** the structure of three fixed PDFs is described in code. A new document version
  needs its config and table specs checked; the tests and table verification say where.

## D-017 Ingest is idempotent on source and code hashes

- `python -m ingest build` skips when `data/manifest.json` records the same SHA-256 for
  every PDF and the same pipeline hash (all `ingest/*.py` files plus the pypdf version).
  Hashing the code means a chunking change can never be served from a stale build, without
  anyone remembering to bump a version.
- The manifest is the corpus freshness record (built_at, source hashes, provenance match
  against `corpus/README.md`, chunk counts, stage timings, throughput). `python -m ingest
  status` exits 1 when the output is stale.
- Files are written to a temporary name and renamed, so an interrupted build leaves the
  previous output intact.

## D-018 The corpus index is built into the worker image (rejected: build at worker start, shared volume)

- `worker/Dockerfile` runs `python -m ingest build` and then `python -m ingest status`, so
  a build that is not fresh fails the image build. The output goes to `/app/data`: chunks,
  FAISS index, manifest, and the embedding model in `data/models/`. `HF_HUB_OFFLINE=1` is
  then set, so a running worker never downloads anything.
- Every worker replica (`--scale worker=N`) gets the same index, byte for byte. Startup
  needs no network and no minute-long embedding step, and the image tag pins the corpus
  version together with the code.
- The PDFs are now in the Docker build context (`.dockerignore` no longer excludes
  `corpus/raw/*.pdf`). A local `data/` is still excluded, so the image never ships a
  developer's build.
- `EMBED_MODEL` is a compose build arg (default `BAAI/bge-small-en-v1.5`). At query time
  the retriever uses the model recorded in the manifest, so the index and the queries
  always use the same model.
- The ingest layer is placed before `retrieval/`, `agent/` and `worker/` are copied, so
  editing those does not rebuild the index.
- Rejected: building at worker start (each replica would spend a minute and need the
  model download, and replicas could diverge). Also rejected: a shared volume filled by
  an init container (one more service, and the index would no longer be versioned with
  the image).
- Cost: the image is larger (onnxruntime, faiss, numpy, plus the model), and the first
  build, including in CI's `docker compose build`, downloads the model.

## D-019 `related()` is part of the Retriever protocol (rejected: agents call HybridRetriever, merge definitions into chunks)

- **Problem:** a table row is only half the rule. IDBI Table-I rows depend on "Number of
  Days" (5.13) and "Third Party Breach" (5.20), and Table-II row 03 on "Negligence of
  Customer" (5.12); these definitions sit pages away from the tables, so a search for the
  row rarely returns them. Ingest records the links in chunk metadata (D-016), but only
  `HybridRetriever.related()` exposed them.
- **Chosen:** `related(doc_id, clause_id) -> list[str]` is in `contracts.tools.Retriever`,
  next to `search_corpus` and `get_clause`. Both agents reach linked clauses through the
  shared interface, so they can be tested against any implementation, and every call goes
  through the graph's wrapper: counted against `AGENT_MAX_TOOL_CALLS` and traced.
- **Rejected: agents call `HybridRetriever.related()` directly.** That ties agents to one
  implementation and bypasses tool-call counting and tracing.
- **Rejected: merge definitions into table chunks.** A citation must name exactly one
  clause and `get_clause` must return exactly that clause (docs/interfaces.md, rules for
  the chunker); merged text would break both.
- **Returns ids, not passages:** the agent decides which links to read with `get_clause`,
  each read counted as one tool call.
