# Agent interfaces

The contracts that let the Investigator (Person 1) and the Verifier plus LangGraph wiring
(Person 2) be built in parallel. All of them live in `contracts/` and are covered by
`tests/test_interfaces.py` and `tests/test_working_days.py`.

## Flow

```
DisputeRequest (validated at the API)
        │
        ▼
 AgentState created by the worker (limits from AGENT_MAX_*)
        │
        ▼
 ┌──────────────┐  InvestigatorOutput  ┌────────────┐
 │ Investigator │ ───────────────────▶ │  Verifier  │
 │  (Person 1)  │                      │ (Person 2) │
 └──────▲───────┘                      └─────┬──────┘
        │                                    │ VerifierOutput.action
        │   retry (retry_strategy,           ├── return   ──▶ RunResult status=completed
        └── attempts += 1) ◀─────────────────┤
                                             └── escalate ──▶ RunResult status=escalated
        any limit hit (steps / tool calls / wall clock) or LLMUnavailableError ──▶ escalate or failed
```

## DocId and Passage (`contracts/agent.py`)

- **What:** `DocId` names the three corpus documents. `Passage` is one retrieved unit of
  text: `doc_id`, `clause_id`, `text`, optional `page` and `score`.
  `Passage.citation_id` gives the `"DOC_ID:clause_id"` string.
- **Implements:** Person 1's chunker and retriever produce Passages.
- **Calls:** both agents. `Passage` maps one-to-one onto the existing `Citation`
  (`doc_id`, `clause_id`, `text`) when building the final `RunResult`.

## InvestigatorOutput (`contracts/agent.py`)

- **What:** the hand-off from Investigator to Verifier. A *draft*: fraud type, fault
  hypothesis, working-day delay and how it was computed, every passage relied on, the
  queries run, a draft route and position, and `cited_clause_ids`.
- **Rule enforced by the model:** every entry in `cited_clause_ids` must equal the
  `citation_id` of a passage in `passages`. A draft cannot cite text it did not retrieve.
- **Implements:** Person 1 (Investigator agent).
- **Calls:** Person 2 (Verifier node reads it from `AgentState.investigator`).

## VerifierOutput (`contracts/agent.py`)

- **What:** the Verifier's judgement: `outcome` (`verified`, `unsupported`,
  `conflicting_provisions`, `out_of_scope`, `contested_fault`), which claims are and are not
  supported, any `modifier_passages` it found, and the next `action`.
- **Rules enforced by the model:**
  - `action="return"` only with `outcome="verified"` and no `unsupported_claims`
    (requirement A3: an unverified answer is never returned).
  - `action="retry"` needs a `retry_strategy`.
  - `action="escalate"` needs an `escalation_reason`.
- **Implements:** Person 2 (Verifier agent).
- **Calls:** Person 2 (graph routing on `action`).

## AgentState (`contracts/agent.py`)

- **What:** the LangGraph state: the request, `run_id`, step and tool-call counters,
  `started_at`, the latest `investigator` and `verifier` outputs, `attempts`, `trace`, and
  the limits `max_steps`, `max_tool_calls`, `max_wall_clock_seconds`. Limits not passed
  explicitly are read from `AGENT_MAX_*` (via `AgentLimits`).
- **Implements:** Person 2 (graph wiring updates the counters and trace).
- **Calls:** both agent nodes read it.
- **Tool-call counting:** the graph increments `tool_call_count` centrally, in the node that
  executes tools, so the limit applies to every agent's calls. Agents never increment it
  themselves.

## LLMClient, LLMResponse, LLMUnavailableError (`contracts/llm.py`)

- **What:** `LLMClient` is a `Protocol` with one method,
  `complete(prompt, *, prompt_version, step, run_id, max_tokens=None) -> LLMResponse`.
  `LLMResponse` carries text, token counts, `cost_usd`, `model`, `latency_ms`,
  `prompt_version` and `step`, recorded at call time.
  `LLMUnavailableError` is raised when the model cannot be reached (circuit open,
  retries exhausted). Callers must escalate or fail, never answer.
- **Implements:** `agent/llm_client.py: HttpLLMClient`. It calls `llm-gateway`
  (`POST /v1/complete`, `LLM_GATEWAY_URL`), which owns the timeout, retries, circuit breaker,
  rate limit and cost accounting (see [llm_gateway.md](llm_gateway.md)). The client makes one
  HTTP request per call and does not retry. Its timeout is
  `LLM_REQUEST_DEADLINE_SECONDS` + 5 s.
- **Calls:** both agents. They receive an `LLMClient` instance; they never import a
  provider SDK.
- **Usage:** construct one client per worker process and reuse it:
  `HttpLLMClient()` (reads `LLM_GATEWAY_URL`). Pass the graph step name as `step`
  (`investigator`, `verifier`) and the run's `run_id`; cost and tokens are then reported per
  step and per run. The correlation id is taken from the worker's bound log context
  automatically. `temperature` is 0 in the gateway.

### When the model is unavailable

**Required behaviour for every agent node:**

- **`LLMUnavailableError`** (breaker open, retries exhausted, rate limited, gateway timed out
  or unreachable): stop the run and return `route=escalate`, `status=escalated` with
  `escalation_reason` "model unavailable". Never return a route the model did not produce,
  never fall back to a default or a heuristic answer.
- **`agent.llm_client.LLMGatewayError`** (the gateway rejected the request: auth, bad
  request, safety block): escalate the same way, with the error's `reason` in the
  escalation reason.

The Verifier treats a missing Investigator answer the same way. The runbook describes the
operational side ([runbook.md](runbook.md), failure mode 1). Not yet implemented in the fake
agent; the real agents must implement it.

## Retriever (`contracts/tools.py`)

- **What:** a `Protocol` with
  `search_corpus(query, k=5, doc_ids=None) -> list[Passage]` (best first),
  `get_clause(doc_id, clause_id) -> Passage | None` (exact lookup) and
  `related(doc_id, clause_id) -> list[str]` (linked clauses).
- **`related` contract:** the clauses linked to this one at ingest, such as the definitions
  a table row depends on, as citation ids `DOC_ID:clause_id` (a link may point to another
  document), each resolvable with `get_clause`. Stable order (as recorded at ingest), no
  duplicates, never the clause itself. A split clause's base id (`RBI_2017_CIRCULAR:5`)
  returns the links of all its parts. Returns `[]` and never raises for an unknown id, a
  heading-only parent (`IDBI_POLICY:5`) or a clause with no links. Links run both ways, so
  a definition also lists the table it qualifies.
  Example: `related("IDBI_POLICY", "Table-I.03")` →
  `["IDBI_POLICY:5.13", "IDBI_POLICY:5.20"]`.
- **Implements:** `retrieval.HybridRetriever`, over the FAISS index and chunks built by
  `python -m ingest build`. Agents depend on the `Retriever` protocol, never on
  `HybridRetriever` directly.
- **Calls:** both agents. The Investigator searches and fetches the definitions a table
  row depends on with `related`. The Verifier uses `get_clause` to re-read cited clauses,
  `related` to check the definitions they rely on, and `search_corpus` to look for
  modifying provisions.
- **Counting:** the graph wraps the retriever, and every call to `search_corpus`,
  `get_clause` or `related` counts as one tool call (limit `AGENT_MAX_TOOL_CALLS`) and is
  logged to the trace.

## Investigator (`agent/investigator`)

- **Call:** `investigate(state, retriever, llm) -> InvestigatorOutput`, from the graph's
  Investigator node (Person 2): `state.investigator = investigate(state, retriever, llm)`.
  `retriever` is the graph's counting wrapper and `llm` the shared `HttpLLMClient`.
- **Reads, never writes, `state`.** Counters and the trace belong to the graph. The
  wrapper logs each search. The Investigator fills `queries_run`.
- **Output:** `passages` holds everything retrieved in this attempt, and
  `cited_clause_ids` the subset the draft relies on. `working_days_delay` and
  `delay_basis` come from `compute_working_days`, never from the model.
- **On a retry** (`state.verifier.action == "retry"`) the Verifier's retry strategy,
  unsupported claims and modifier passages, and the previous draft position, are shown to
  the model. Each attempt searches afresh.
- **Raises (not caught; the graph escalates):** `ToolBudgetExceeded`,
  `LLMUnavailableError` ("model unavailable"), `agent.llm_client.LLMGatewayError`, and
  `agent.investigator.InvestigatorError` (malformed reply, no evidence, or wall-clock
  budget spent before a turn). `str(exc)` is a usable escalation reason.
- **Cost:** 2 LLM calls normally, 3 at most (`step="investigator"`), and about 2 to 8 tool
  calls. Design: [decisions.md](decisions.md) D-020.

## Verifier (`agent/verifier`)

- **Call:** `verify(state, draft, retriever, llm, *, prompt_version="v1") -> VerifierOutput`,
  from the graph's verify node with `draft = state.investigator`, the graph's counting
  retriever and the shared LLM client. `verify_detailed(...)` returns the same output plus
  the draft's citations that passed the citation check (`Verification.citations`), which the
  graph cites in the result.
- **Reads, never writes, `state`.** It uses `state.request` (claims, dates),
  `state.run_id`, `state.started_at` and `state.max_wall_clock_seconds`.
- **Checks, in order:** citations (code), hard rules (code), linked clauses (code), then at
  most 2 model calls (`step="verifier"`): "check" (claims, conflict searches) and "judge"
  (only if the searches found new passages). Details: [agents.md](agents.md).
- **Output rules:** `return` only for `verified` with nothing unsupported; `retry` (with a
  `retry_strategy`) when claims are unsupported; `escalate` (with a reason) for hard rules,
  conflicts, an escalate draft or a malformed model reply. For a conflict or a boundary
  delay, `modifier_passages` holds the other side's clauses, so the result cites both.
- **Raises (not caught; the graph escalates):** `ToolBudgetExceeded`,
  `LLMUnavailableError`, `LLMGatewayError`, and `agent.verifier.VerifierError` (wall-clock
  budget spent before a model call). A malformed model reply is not raised: it becomes
  outcome `unsupported`, action `escalate`.
- **Prompts:** `agent/prompts/verifier_<version>.yaml`, loaded with
  `agent.prompts.load_prompts("verifier_<version>")`.

## Graph (`agent/graph.py`)

- **Call:** `run_graph(req, run_id, retriever, llm, *, settings=None, **limits) -> RunResult`,
  from `agent.runner` when `AGENT_MODE=graph`. `prompt_version` is
  `inv-<investigator version>+ver-<verifier version>`.
- **State:** `GraphState`, an `AgentState` plus `halt_reason` (set by a node that stopped the
  run), `verified_citations` and the final `result`.
- **State transitions:**

| Node | Reads | Writes |
|---|---|---|
| investigate | request, `investigator` and `verifier` (on a retry) | `investigator`, `step_count`, `tool_call_count`, `trace`, or `halt_reason` |
| verify | request, `investigator`, `attempts` | `verifier`, `verified_citations`, `attempts` (+1 on a retry), `step_count`, `tool_call_count`, `trace`, or `halt_reason` |
| finalize | everything | `result`, `trace` |

  Edges: investigate → verify, or finalize if `halt_reason`; verify → investigate if
  `verifier.action == "retry"`, else finalize; finalize → END. A retry with
  `attempts >= AGENT_MAX_VERIFY_RETRIES` is turned into `escalate` by the verify node.
- **Counters:** `step_count` counts agent node executions (limit `max_steps`);
  `tool_call_count` mirrors the shared retriever wrapper (limit `max_tool_calls`).

## compute_working_days (`contracts/tools.py`)

- **What:** `compute_working_days(start, end, home_state, holidays=None) -> int`. Counts
  working days after `start` up to and including `end`. Sundays and the 2nd and 4th
  Saturdays are non-working, plus any `holidays` passed in. Takes `date`, not
  `datetime`: convert to the home branch's local date first. Deterministic, no LLM.
- **Implemented now.** State holiday calendars are a TODO (`home_state` is accepted
  for that).
- **Calls:** the Investigator (to fill `working_days_delay`) and the Verifier (to
  recompute and check it). For the delay, `start` is the date of the bank's
  communication (`bank_alert_datetime`), `end` the date of reporting.

## Clause ID conventions

`clause_id` is the document's own numbering, written exactly as below so the chunker,
`get_clause` and the Verifier agree. No spaces. Roman numerals lower-case in brackets.
Citation form: `DOC_ID:clause_id`, for example `IDBI_POLICY:5.13`.

### RBI_2017_CIRCULAR

Numbered paragraphs 1 to 14, some with sub-items `(i)`, `(ii)`, … The opening
unnumbered paragraph is paragraph 1.

| Text | clause_id |
|------|-----------|
| Paragraph 6 (zero liability, lead-in) | `6` |
| Paragraph 6 (ii) (third-party breach, within three working days) | `6(ii)` |
| Paragraph 7 (i) (customer negligence) | `7(i)` |
| Paragraph 7 (ii) (four to seven working days) | `7(ii)` |
| Table 1 (maximum liability by account type) | `Table-1` |
| "Further, if the delay in reporting is beyond seven working days…" (follows Table 1, part of para 7) | `7(ii)-beyond-7` |
| Table 2 (summary of customer liability) | `Table-2` |
| Note under Table 2 ("…excluding the date of receiving the communication") | `Table-2-note` |
| Annex, row Sr. No. 4 | `Annex-4` |

### IDBI_POLICY

Numbered sections with decimal sub-clauses. Definitions are `5.1` to `5.23`.

| Text | clause_id |
|------|-----------|
| Definition "Negligence of Customer" | `5.12` |
| Definition "Number of Days" | `5.13` |
| Definition "Third Party Breach" | `5.20` |
| Definition "Vishing" | `5.22` |
| Table - I (third-party breach by reporting delay) | `Table-I` |
| Table - I, one row (S. No 01 to 10) | `Table-I.01` … `Table-I.10` |
| Paragraph after Table - I ("…beyond 60 calendar days…") | `Table-I-note` |
| Table-II (bank fault / after reporting / customer negligence) | `Table-II` |
| Table-II, one row | `Table-II.01` … `Table-II.03` |
| Lettered or roman sub-items | `3.1(i)`, `8.1(v)` |

### RBI_IOS_2026

Numbered clauses with bracketed sub-clauses and lettered items.

| Text | clause_id |
|------|-----------|
| Clause 8, sub-clause 3 (compensation limits) | `8(3)` |
| Clause 10, sub-clause 1, item g (90-day limit) | `10(1)(g)` |
| Clause 10, sub-clause 2 (excluded matters) | `10(2)` |
| Definitions | `3(1)(f)` etc. |
| Annex, Part A, point 2 | `Annex-A.2` |

### Rules for the chunker

- Every chunk has exactly one `clause_id`. A chunk never spans two clause_ids. Long
  clauses may be split into `5.12#1`, `5.12#2`. The Verifier treats `#n` parts of the same
  clause as one clause.
- A table is its own clause (`Table-I`), and each row is also retrievable (`Table-I.03`).
  Keep the table's banner text (the reporting-delay band) with its rows.
- Definitions that a table depends on ("Number of Days", "Third Party Breach",
  "Negligence of Customer") stay separate clauses. Link them in chunk metadata, not by
  merging text, so `get_clause` still returns exactly the defined clause.
- `page` is the 1-based PDF page index, not the number printed on the page. The IDBI PDF's
  printed numbers are offset by 2.

## Citation rules

A citation (`contracts.Citation`) is `DOC_ID:clause_id` plus `text`. The evaluation checks
it against the chunk the retriever returns, not the raw PDF (requirement A3: every citation
resolves to retrieved text). Code: `eval/chunks.py`; metric: `citation_resolution_rate`.

- **`text` is a short verbatim span copied from that chunk's `text`**, at most 60 words.
  Copy it, do not paraphrase, summarise or re-quote it from memory. Matching ignores
  whitespace and typographic quotes and dashes (as `eval/corpus.py`'s "verbatim"), nothing
  else.
- **Quote the chunk, not the PDF.** Some chunks are worded differently from the page:
  table rows are labelled by column ("Type of Account: BSBD Accounts"), running page
  headers are dropped from clauses that cross a page break, and IDBI bullet glyphs are
  removed. Text that is in the PDF but not in the chunk does not resolve.
- **Split clauses: cite the base clause id.** A long clause is stored as parts
  (`RBI_2017_CIRCULAR:5#1`, `5#2`). Cite it as `RBI_2017_CIRCULAR:5`, the id the scenarios,
  `supporting_clause_recall` and `must_not_cite` use. The citation resolves if the text is
  in any part (or runs across two consecutive parts). A `#n` suffix is accepted and treated
  as the base clause.
- **Cite a clause that has its own chunk.** Section headings with no text of their own
  (`IDBI_POLICY:5`, `RBI_IOS_2026:14`) are not chunks and do not resolve; cite the
  sub-clause (`IDBI_POLICY:5.20`). A clause with its own text and sub-items, such as
  `RBI_2017_CIRCULAR:7` or a whole table `IDBI_POLICY:Table-I`, does resolve, but cite the
  most specific clause that holds the quoted text (`7(ii)`, `Table-I.03`).
- A failing citation is reported with one reason: `unknown_clause` (no chunk with that id),
  `too_long` (over 60 words) or `text_not_in_chunk`.
