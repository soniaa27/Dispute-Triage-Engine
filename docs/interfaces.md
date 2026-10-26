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
- **Implements:** whoever owns `llm_gateway/` (Person B in the README layout). That means a
  client for `llm-gateway`, with timeouts, retries and the circuit breaker. Not built yet.
  Prices come from `LLM_PRICE_PER_1K_*`.
- **Calls:** both agents. They receive an `LLMClient` instance; they never import a
  provider SDK.

## Retriever (`contracts/tools.py`)

- **What:** a `Protocol` with
  `search_corpus(query, k=5, doc_ids=None) -> list[Passage]` (best first) and
  `get_clause(doc_id, clause_id) -> Passage | None` (exact lookup).
- **Implements:** Person 1, over the FAISS/Chroma index built by `python -m ingest build`.
- **Calls:** both agents. The Investigator searches. The Verifier uses `get_clause` to
  re-read cited clauses and `search_corpus` to look for modifying provisions.

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
