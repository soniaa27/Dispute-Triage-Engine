# Evaluation set

A fixed set of dispute scenarios (requirement L3). Each has its expected outcome and the corpus
passage that supports it, and the whole set runs as a batch from the command line. It is the
yardstick for prompt variants (L2), for the before/after change (L3) and for scoring live runs (L6).

Expected outcomes come from the text of the PDFs in `corpus/raw/`, extracted with pypdf.
Liability caps and day limits come from the **25 September 2025** IDBI policy.

## Commands

```bash
make eval-validate                      # python -m eval.validate
make eval                               # python -m eval.run_eval (all scenarios, label "current")
make eval ARGS="--prompt-version v1"    # label the run (used in the report file name)
make eval ARGS="--ids S01,S14 --limit 5"
make eval-compare A=eval/reports/<a>.json B=eval/reports/<b>.json
```

`run_eval` options:

| Option | Meaning |
|--------|---------|
| `--scenarios` | Scenario file (default `eval/scenarios.jsonl`) |
| `--prompt-version` | Label for the run |
| `--limit` | Run only the first N scenarios |
| `--ids` | Comma-separated IDs to run |
| `--out` | Report directory (default `eval/reports/`, git-ignored) |
| `--timeout` | Per-scenario limit in seconds (default `RUN_TIMEOUT_SECONDS` or 90) |
| `--judge-client` | `gateway` (HttpLLMClient to llm-gateway; `make eval` points it at `localhost:8100`) or `module:factory` returning a `contracts.llm.LLMClient`, for the four judged metrics |
| `--agent-mode` | `fake`, `investigator` or `graph` (default `AGENT_MODE`, else `fake`). `graph` runs the Investigator and Verifier in-process with the local index (`make ingest`) and the model behind llm-gateway at `LLM_GATEWAY_URL` |
| `--confirm-live` | Required when a real agent would call a provider other than the stub (it spends quota). Without it the runner stops before the first call |

The runner calls `agent.runner.run_agent` directly, in-process, with whatever implementation
is current. Each run writes `eval/reports/<UTC timestamp>_<label>.json` (full per-scenario
results and metrics) and a `.md` summary.

The eval tooling needs `pypdf` and `cryptography` from `requirements-dev.txt`. The IDBI PDF is
AES-encrypted (no password), and pypdf needs `cryptography` to read it.

## Scenario schema (`eval/schema.py`)

| Field | Content |
|-------|---------|
| `id` | `S01`…`S24` |
| `title`, `category` | Short description; one of the categories below |
| `request` | A full `DisputeRequest`, all datetimes with `+05:30` |
| `expected.fraud_type` | `FraudType` |
| `expected.fault_hypothesis` | `FaultHypothesis` |
| `expected.working_days_delay` | `compute_working_days(bank_alert date, reported date, home_branch_state)`; `None` only if there is no alert |
| `expected.route` | `Route` |
| `expected.must_escalate` | `true` exactly when `route` is `escalate` (enforced) |
| `expected.customer_liability` | `zero`, `limited`, `full`, `per_bank_policy`, `not_applicable`, `undetermined` |
| `expected.liability_cap_inr` | Cap from the table for `limited`, else `null` |
| `expected.supporting_clauses` | `DOC_ID:clause_id` per `docs/interfaces.md`. **The first is primary**: `supporting_passage` comes from its document |
| `expected.supporting_passage` | Verbatim excerpt (at most 60 words) |
| `expected.must_not_cite` | Clauses whose citation shows a wrong reasoning path |
| `expected.basis_must_mention` | Optional. Terms the agent's `liability_position` + `basis` must contain, ignoring case, commas and whitespace (a stem like `compensat` matches "compensated" and "compensation"). Each term must appear in `supporting_passage`. Used by S07 so the decline states the ₹2,500 compensation |
| `rationale` | 1 to 3 sentences |

`undetermined` is an addition to the requested liability values. It marks escalations
(conflicts, boundaries) where the corpus does not settle liability, as distinct from
`not_applicable` (out of scope).

### Route mapping used by the set

The corpus decides liability, not routes. The set maps liability positions to routes as
follows. This mapping is a team decision; change the scenarios if you change it.

| Situation | Liability | Route |
|-----------|-----------|-------|
| Bank fault, third-party breach within 3 working days, or transaction after reporting | zero | `provisional_credit` (shadow credit, IDBI 7.1) |
| Third-party breach, 4 to 7 working days | limited, capped | `provisional_credit` (shadow credit restricted to the bank's liability, Table-I footnote) |
| Third-party breach, beyond 7 working days up to 60 calendar days | per_bank_policy (IDBI: full, compensation up to ₹2,500) | `decline` (no shadow credit). The basis **must** state the compensation; checked by `basis_must_mention` |
| Third-party breach, beyond 60 calendar days | full | `decline` |
| Customer negligence, before reporting | full | `decline` |
| Duplicate charge on a card | zero | `chargeback` |
| Conflict, contested fault, boundary, out of scope | undetermined or not_applicable | `escalate` |

## Categories and coverage

| Category | Scenarios | Count |
|----------|-----------|-------|
| clear_cut | S01, S02, S04, S05, S06 | 5 |
| route_coverage | S03, S07, S08, S11, S12 | 5 |
| negligence | S09, S10 | 2 |
| fraud_type_coverage | S13 | 1 |
| conflict_escalation | S14 (section 4.1), S15 (SIM swap), S16 (contested OTP) | 3 |
| boundary | S17 (exactly 3 days), S18 (exactly 7 days) | 2 |
| out_of_scope | S19 (ATM cash not dispensed), S20 (NEFT to wrong payee), S21 (duress) | 3 |
| adversarial | S22, S23, S24 (prompt injection in `complaint_text`) | 3 |

Routes: provisional_credit 8, decline 6, escalate 9, chargeback 1.
Fraud types: card_not_present_fraud 11, skimming 5, upi_social_engineering 2,
merchant_dispute 1, duplicate_charge 1, unknown 4.

## Validation (`python -m eval.validate`)

For every scenario:

1. It parses against the schema, including the full `DisputeRequest` contract.
2. `working_days_delay` equals `compute_working_days` on the request's dates.
3. Every clause ID in `supporting_clauses` and `must_not_cite` matches the
   `docs/interfaces.md` conventions (patterns in `eval/corpus.py`).
4. `supporting_passage` appears verbatim in the extracted text of the primary clause's
   document. *Verbatim* means equal after NFKC, mapping typographic quotes and dashes to
   ASCII, and **removing all whitespace**. pypdf inserts spaces inside words
   ("wor king", "Rs. 10 ,000"), so collapsing whitespace alone would reject correct passages.
5. No clause is both supporting and must-not-cite.
6. Every `basis_must_mention` term appears in `supporting_passage`, so the required wording
   is itself grounded in the corpus.

7. When the chunk output exists (`make ingest`), every `supporting_clauses` id exists as a
   chunk, and `supporting_passage` is in the primary clause's chunk text. For a table row,
   whose chunk is relabelled by column, every number in the passage (amounts, day counts,
   row number) must be in the row chunk instead. Without the chunk output these checks are
   skipped with a warning, not a failure.

Then for the set: at least 20 scenarios, and every route, every fraud type (except
`unknown`) and every category present. Exits 1 on any failure.

The check is document-level. It proves the passage exists in that PDF, not that it sits under
the cited clause number.

## Metrics (`eval/metrics.py`)

N is the number of scenarios run. A run that failed or timed out counts as wrong on every
per-scenario metric. A ratio with a zero denominator is reported as `null`.

### Deterministic: computed on every run

| Metric | Definition |
|--------|------------|
| `route_accuracy` | #(returned route = expected route) / N |
| `escalation_precision` | TP / (TP + FP), where predicted positive = returned route is `escalate`, actual positive = `must_escalate` |
| `escalation_recall` | TP / (TP + FN) |
| `fraud_type_accuracy` | #(returned fraud_type = expected) / N |
| `citation_resolution_rate` | #(returned citations that resolve to retrieved text) / #(returned citations). Resolves = the clause has a chunk in `data/` and `text` is a verbatim span of that chunk's text, at most 60 words ("Citation rules" in [interfaces.md](interfaces.md)). Needs `make ingest`. Each failing citation is listed per scenario as `citation_failures` with a reason (`unknown_clause`, `too_long`, `text_not_in_chunk`); totals in `citation_failures_by_reason` |
| `citation_hallucination_rate` | 1 − `citation_resolution_rate`: the deterministic part of hallucination |
| `pdf_verbatim_rate` | #(returned citations whose `text` is verbatim, as defined above, in the PDF named by their `doc_id`) / #(returned citations). **Informational only, not used for scoring**: correct quotes of table rows and of clauses that cross a page break are not PDF text |
| `supporting_clause_recall` | Σ #(expected supporting clauses among returned `doc_id:clause_id`) / Σ #(expected supporting clauses), micro-averaged; `#n` chunk suffixes are ignored |
| `must_not_cite_violations` | Count of returned citations listed in the scenario's `must_not_cite` (per-scenario list also reported) |
| `injection_resistance` | #(adversarial scenarios with route = expected) / #(adversarial scenarios) |
| `basis_mention_rate` | #(scenarios with `basis_must_mention` whose answer contains every term) / #(such scenarios). Missing terms are listed per scenario as `basis_terms_missing` |
| `runs_failed` | Runs with no result or an error |

### LLM-judged: the four L3 metrics

Implemented in `LLMJudge`, using any `contracts.llm.LLMClient` passed via `--judge-client`;
`--judge-client gateway` judges with the model configured in llm-gateway, and the report
records it as `judge_model`. Judge calls appear in `llm_calls` under `judge-<run_id>`.
With no client they are reported as `not computed (no LLM client configured)`; no numbers are
made up. The judge sees the request, the agent's answer and the passages the agent cited,
never the expected answer. Judge prompts are versioned (`judge-v1`) and treat customer text
as data.

| Metric | Definition |
|--------|------------|
| `groundedness` | The judge splits `liability_position` + `basis` into atomic claims and labels each `supported`, `unsupported` or `contradicted` against the cited passages (and request facts for case facts). groundedness = #supported / #claims |
| `context_relevance` | For every cited passage, the judge rates relevance to deciding liability for this dispute in [0, 1]. Mean over all passages |
| `faithfulness` | Per scenario, the judge answers whether the route and liability position follow from the cited passages applied to the request facts without contradicting them (1 or 0). Mean over scenarios |
| `hallucination_rate` | #(claims labelled `unsupported` or `contradicted`) / #claims. Reported alongside the deterministic `citation_hallucination_rate` |

Groundedness and hallucination rate come from the same claim labels. Faithfulness judges
the *decision*, while groundedness judges the *wording*.

TruLens was considered and not used; see `docs/decisions.md` D-010.

## Retrieval metrics (`retrieval/evaluate.py`)

Scores the retriever alone, with no agent and no LLM: does it find each scenario's
`supporting_clauses`? Needs a built corpus (`make ingest`).

```bash
make retrieval-eval                     # python -m retrieval.evaluate
make retrieval-eval ARGS="--k 3 5 --modes hybrid --no-write"
```

Every mode is scored: `hybrid` (what `search_corpus` uses), `dense` (FAISS only) and
`keyword` (BM25 only). Queries come from a named source (`--queries`):

- `complaint` uses the scenario's `complaint_text` unchanged.
- `investigator` runs the real Investigator through llm-gateway (default
  `http://localhost:8100`, or `--gateway`) and scores the searches it chose
  (`queries_run`). It makes about 2 LLM calls per scenario, at 4 calls a minute, so use
  `--ids S01 S09` for a quick check. Runs that fail are listed under `query_failures`.

When a source gives several queries, their rankings are merged by RRF. A retrieved `5#1`
counts as clause `5`.

| Metric | Definition |
|--------|------------|
| `recall@k` | Σ #(supporting clauses in the top k) / Σ #(supporting clauses), micro-averaged |
| `hit@k` | #(scenarios with at least one supporting clause in the top k) / N |
| `primary_recall@k` | #(scenarios whose first supporting clause is in the top k) / N |
| `must_not_cite@k` | Count of `must_not_cite` clauses in the top k, over all scenarios |
| `mrr` | Mean of 1 / rank of the first supporting clause (0 if not in the top 50) |
| `latency_ms` | p50 and p95 of one search call |

Each run writes `eval/reports/<UTC timestamp>_retrieval-<source>.json` (the queries, and
the rank of every supporting and must-not-cite clause, per scenario) and a `.md` summary.

With `complaint` queries, recall is low (hybrid recall@10 is about 0.10 with bge-small).
Customers do not state their account type or how many working days passed, and those facts
pick the Table-I row and the RBI paragraph. The number is a floor, not a target. The
Investigator's LLM chooses its own searches (requirement A1) and can see the request
fields.

## Limitations

- **State holidays are not modelled.** `compute_working_days` excludes Sundays and 2nd/4th
  Saturdays only. Bank holidays by `home_branch_state` are not yet loaded (D-009), so a delay
  spanning a holiday is over-counted by a day. Scenarios whose band depends on the count use
  date windows clear of major holidays (S05 was moved off Janmashtami). S08's window includes
  Holi, which does not matter because it is far past 60 calendar days either way.
- **The RBI discretionary waiver is not modelled.** RBI circular paragraph 9 lets a bank waive
  customer liability at its discretion, even in negligence cases. The set expects the
  table outcome (S09, S10, S22: full liability, decline), not a waiver.
- **Routes are a mapping, not corpus text.** The documents decide liability; the route
  mapping above is a team decision (D-011). Chargeback in particular (S12) has no clause.
- **Passage checks are document-level.** Validation proves a passage is in the right PDF,
  not under the cited clause number.
- **Basis checks are keyword checks.** `basis_must_mention` confirms the terms appear, not
  that the sentence around them is correct; that is the judged metrics' job.

## How the set is used

- **L2, prompt variants:** run the set once per prompt version
  (`--prompt-version v1`, then `v2`), then `make eval-compare A=… B=…`. Report the metric
  that moved and by how much.
- **L3, before/after change:** run, make the one deliberate change (for example
  definition-aware chunking instead of fixed-size chunks), run again, and compare. Report all
  four judged metrics plus the deterministic ones. Compare warns if the two reports used
  different scenario files.
- **L6, live runs:** see [Quality over time](#quality-over-time-live-runs) below.
- **Changing the set:** never edit a scenario in place between runs being compared. Add new
  IDs instead. Reports record the scenario file's SHA-256.

## L2 and L3: prepared runs

Everything below is ready but has **not been run against a real model**. Reports now record
`agent_mode`, `provider` (what llm-gateway reports), `agent_models` (from the runs' traces),
`agent_prompt_versions` (`inv-v1+ver-v1` or `inv-v1+ver-v2`), `verifier_prompt_version`,
`retrieval_downrank_tables` and the judge model, so two reports show what differed.

**The two switches** (environment variables, read where the agent runs: the eval process
for `make eval`, the worker for the stack):

| Variable | Values | What changes |
|----------|--------|--------------|
| `AGENT_VERIFIER_PROMPT_VERSION` | `v1` (default), `v2` | L2. `agent/prompts/verifier_v2.yaml` differs from v1 in one place: rule 3 of the check prompt. v1 accepts a claim if a passage supports it; v2 accepts it only if one passage states it explicitly, including its specific value (fault category, delay band, amount or cap), and rejects support that needs inference across passages. The judge prompt and both reply formats are identical (`tests/test_eval_variants.py` checks this) |
| `RETRIEVAL_DOWNRANK_TABLES` | `false` (default), `true` | L3. Hybrid search multiplies the fused score of RBI Annex rows (`RBI_2017_CIRCULAR:Annex-1` ... `Annex-8`) and whole-table chunks (`Table-1`, `Table-2`, `Table-I`, `Table-II`) by 0.5, so specific clauses and table rows rank above them. Table rows, notes and the Ombudsman Scheme's Annex are unchanged (`retrieval/hybrid.py`) |

### 1. Free dry run on the stub

Checks the whole path (graph, judge, report fields) without spending quota. The stub answers
the agents from the scenarios (docs/load_test.md), so the scores say nothing about quality.

```bash
make ingest                                           # local index, once (no model calls)
LLM_PROVIDER=stub docker compose up -d --wait         # gateway on the stub
make eval ARGS="--agent-mode graph --prompt-version stub-dryrun --ids S01,S14 --judge-client gateway"
```

### 2. L3 retrieval check, free

Retrieval alone, before and after, with no model:

```bash
make retrieval-eval
RETRIEVAL_DOWNRANK_TABLES=true make retrieval-eval
```

### 3. Live runs (your quota)

Start the stack with your own `.env` (`LLM_PROVIDER=gemini`), then run each variant with the
same scenarios. The agent runs in the eval process, so its limits come from the shell; the
values below fit 4 model calls per run at 4 requests per minute (docs/agents.md, "Limits").

```bash
SUBSET=S01,S04,S07,S09,S12,S14,S16,S17,S19,S22
LIVE="AGENT_MAX_WALL_CLOCK_SECONDS=120 AGENT_MAX_TOOL_CALLS=40 LLM_REQUEST_DEADLINE_SECONDS=40"

# Baseline: Verifier v1, retrieval unchanged. Serves as the L2 baseline AND the L3 "before".
env $LIVE AGENT_VERIFIER_PROMPT_VERSION=v1 make eval ARGS="--agent-mode graph --confirm-live \
  --prompt-version ver-v1 --ids $SUBSET --timeout 175 --judge-client gateway"

# L2 variant: Verifier v2
env $LIVE AGENT_VERIFIER_PROMPT_VERSION=v2 make eval ARGS="--agent-mode graph --confirm-live \
  --prompt-version ver-v2 --ids $SUBSET --timeout 175 --judge-client gateway"

# L3 after: retrieval down-ranking on, Verifier v1
env $LIVE RETRIEVAL_DOWNRANK_TABLES=true make eval ARGS="--agent-mode graph --confirm-live \
  --prompt-version l3-downrank --ids $SUBSET --timeout 175 --judge-client gateway"

make eval-compare A=eval/reports/<ts>_ver-v1.json B=eval/reports/<ts>_ver-v2.json       # L2
make eval-compare A=eval/reports/<ts>_ver-v1.json B=eval/reports/<ts>_l3-downrank.json  # L3
```

Run them on the same day and model if possible: model updates and the provider's own
variation move scores too. Temperature is 0, but runs are not bit-for-bit repeatable.

### Call budget

Counted by driving every scenario through the graph on the stub (the happy path: two
Investigator turns, and a Verifier pass only where no hard rule decides). A judged run costs
2 + (citations) calls (D-027). A real model adds a third Investigator turn or a retry now
and then: allow 10 to 20% more agent calls.

| Run | Agent calls | Judge calls | Total | At 4 per minute |
|-----|------------:|------------:|------:|----------------:|
| All 24 scenarios, one variant | 78 (15 x 4 + 9 x 2) | 106 | 184, about 200 with a real model | about 50 min |
| L2 (v1 and v2) + L3 after, all 24 | 3 x 78 | 3 x 106 | about 600 | about 2.5 h |
| The 10-scenario subset above, one variant | 32 | 47 | 79, about 85 | about 22 min |
| Subset, 3 runs (L2 and L3) | 96 | 141 | about 255 | about 1 h 10 min |

Budget-friendly plan:

1. **Reuse the baseline**: one v1 run is both the L2 baseline and the L3 "before" (3 runs,
   not 4).
2. **Use the 10-scenario subset**: it keeps every category (clear-cut, route coverage,
   negligence, chargeback, contested fault, boundary, out of scope, adversarial).
3. **Do the free checks first**: the stub dry run and `make retrieval-eval` before and
   after catch mistakes without quota.
4. **One run per day if the daily quota is small**: about 85 calls each. Check the model's
   requests-per-day limit in Google AI Studio; per-minute limits only stretch the time.
5. **Where L2 can show a difference**: v1 and v2 differ only in the Verifier's check call,
   which runs for the cases no hard rule decides (the clear-cut ones). If quota is very
   short, the L2 pair can use only S01, S04, S07, S09, S12, S22 (about 54 calls each).

A second free provider would double the daily budget, but none is added: that needs a new
provider in llm-gateway, and the scores would then mix two models.

## Quality over time (live runs)

Live runs have no expected answer, so only the reference-free judged metrics apply:
groundedness, context relevance, faithfulness and hallucination rate, with the same prompts
(`judge-v1`) and definitions as above (`LLMJudge.judge_run` scores one run; `score` sums them).

- **What runs:** the `quality` service (`python -m quality run`, worker image) wakes every
  `QUALITY_INTERVAL_SECONDS` (900), picks finished runs from the last
  `QUALITY_LOOKBACK_HOURS` (24) that are in the sample and not yet scored, and judges up to
  `QUALITY_MAX_PER_PASS` (1) of them. A run is in the sample when
  `sha256(run_id)` as a fraction is below `QUALITY_SAMPLE_RATE` (0.2): reproducible, no state.
  Runs without citations are skipped (nothing to ground).
- **Where it goes:** one row per run in `quality_scores` (claim counts, the four scores,
  judge model, run status, route and prompt version, or the judge error).
- **Chart:** `http://localhost:8000/quality.html` (linked from the web client): headline
  tiles, one line per metric per hour or day, analyst feedback for the same period, and a
  table view. The data is `GET /quality?days=7&bucket=day` (`bucket=hour` up to 14 days),
  which also breaks the scores down by prompt version, so a new version shows as a step.
- **Cost:** 2 + (citations) model calls per judged run through llm-gateway, recorded in
  `llm_calls` as `judge_*` steps under `judge-<run_id>`. At the defaults that is at most 4
  runs an hour.
- **When the model is unavailable** the pass stops and writes nothing; the run is judged on
  a later pass. An unusable judge reply is stored as an error row and left out of averages.
- **By hand:** `docker compose exec quality python -m quality once` (one pass now) and
  `docker compose exec quality python -m quality report` (a table per day).
- With `LLM_PROVIDER=stub` every judgement is an error row: the stub's reply is not JSON.

