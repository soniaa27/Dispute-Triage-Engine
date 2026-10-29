# Agents: Investigator, Verifier and the graph

How a dispute becomes a recommendation in `AGENT_MODE=graph` (requirements A1, A2, A3, F5
to F7). Contracts: [interfaces.md](interfaces.md). Design records: [decisions.md](decisions.md)
D-020 to D-024.

## Topology

```
 DisputeRequest
      │
      ▼
 ┌─────────────┐  InvestigatorOutput  ┌──────────┐
 │ investigate │ ───────────────────▶ │  verify  │
 └─────▲───────┘                      └────┬─────┘
       │                                   │ VerifierOutput.action
       │  retry (VerifierOutput with       ├── return ───────┐
       └── retry_strategy; at most         ├── escalate ─────┤
           AGENT_MAX_VERIFY_RETRIES) ◀─────┘                 ▼
                                                       ┌──────────┐
 any node that stops the run (limit, model ──────────▶ │ finalize │ ──▶ RunResult
 unavailable, agent error)                             └──────────┘
```

`agent/graph.py` builds a LangGraph `StateGraph` over `GraphState` (an `AgentState` plus the
reason a node stopped the run, the Verifier's checked citations and the final `RunResult`).
`agent/runner.py` runs it for `AGENT_MODE=graph`; `fake` and `investigator` still work.

| From | Condition | To |
|---|---|---|
| START | | investigate |
| investigate | draft produced | verify |
| investigate | the run was stopped (see failures) | finalize |
| verify | `action="return"` | finalize → `completed` |
| verify | `action="escalate"` | finalize → `escalated` |
| verify | `action="retry"` and retries left | investigate (attempts + 1) |
| verify | `action="retry"`, no retries left | the graph turns it into `escalate` → finalize |
| verify | the run was stopped | finalize → `escalated` |

## What each agent owns

**Investigator** (`agent/investigator`, D-020). Classifies the fraud, forms the fault
hypothesis, takes the working-day delay from code, chooses its own searches and lookups,
and drafts route, position and citations. 2 model calls normally, 3 at most. On a retry it
sees the Verifier's strategy, unsupported claims and modifying provisions.

**Verifier** (`agent/verifier`, D-022). Decides whether the draft may be returned. Its
stages:

| Stage | Done by | What |
|---|---|---|
| a. citations | code | each cited clause exists (`get_clause` on the base id) and the quoted text is a verbatim span of at most 60 words, the same rules as `eval/chunks.py` |
| b. linked clauses | code | `related()` of each cited clause, then `get_clause` of each new link, once; stops while 3 tool calls are left for stage d |
| c. hard rules | code | always escalate, no model call (below). Run right after stage a: when they, a failed citation or an escalate draft already decide, stage b is skipped to save tool calls |
| d. check | LLM call 1 | each claim supported or not, with the clause it rests on; up to 3 searches for provisions that could modify or conflict with the draft, run with k=5 |
| e. judge | LLM call 2 | only if stage d's searches found passages not already shown: does any modify or conflict with the draft |
| f. decide | code | outcome and action |

**The graph** owns the shared tools, the limits, the trace and the retry count. It wraps
the retriever and the LLM client once per run (`agent/tracking.py`), so both agents share
one tool-call budget and one trace.

## The hand-off

`InvestigatorOutput` (`contracts/agent.py`) goes from investigate to verify through
`state.investigator`. `VerifierOutput` goes back through `state.verifier`, and the
Investigator reads it on a retry. Neither agent writes to the state; the graph's nodes
do. A draft can cite only passages it retrieved (enforced by `InvestigatorOutput`), and
the Verifier re-reads each from the retriever rather than trusting the draft's copy.

## Decision (stage f)

| Situation | Outcome | Action |
|---|---|---|
| a hard rule fires | the rule's outcome (below) | escalate |
| the draft itself routes to `escalate` | unsupported | escalate |
| a citation fails, or the draft's delay differs from the recomputed one | unsupported | retry |
| the check reply marks a claim unsupported, or rests it on a clause that was not shown | unsupported | retry |
| the check or judge reply is malformed | unsupported | escalate |
| the judge finds a conflict | conflicting_provisions (`modifier_passages` = the conflicting clauses) | escalate |
| every claim supported, no new passage, or the judge finds no conflict | verified | return |

`VerifierOutput`'s validators hold throughout: `return` only with `verified` and no
unsupported claims, `retry` with a strategy, `escalate` with a reason.

A malformed reply escalates instead of retrying: the fault is the Verifier's model call, and
redrafting would cost 2 more Investigator calls for nothing.

## Hard rules (stage c)

Priority order; the first hit sets the outcome, every hit is in the reason. Full rationale
and the rules considered and rejected are in `agent/verifier/rules.py`.

1. **Out of scope** (`out_of_scope`): the draft cites no clause; or the fraud type is
   `unknown` and no citation resolves.
2. **Boundary delay** (`conflicting_provisions`): the recomputed delay is exactly 3 or 7
   working days (D-009). RBI `Table-2-note` and IDBI `5.13` are added as modifier passages,
   so the result cites both counting rules.
3. **Fault unclear** (`contested_fault`): `fault_hypothesis` is `unclear`.
4. **The customer's statements contradict the fault** (`contested_fault`):
   - `shared_otp_or_pin` is true but the draft blames the bank or a third party (S14);
   - `shared_otp_or_pin` is false but the draft finds customer negligence (S15, S16).

Given each scenario's expected draft, these rules fire on exactly the scenarios that must
escalate (`tests/test_verifier.py`).

**Rejected:** escalating on `received_alert` false (S07 and S08 expect decline), on
`card_in_possession`, on complaint keywords, on more than 60 calendar days, or on a missing
alert time. No document or scenario supports them.

## Limits

| Limit | Setting (default) | Enforced |
|---|---|---|
| Node executions | `AGENT_MAX_STEPS` (12) | before each agent node |
| Tool calls, both agents together | `AGENT_MAX_TOOL_CALLS` (20) | by the retriever wrapper, before the call over the limit |
| Wall clock | `AGENT_MAX_WALL_CLOCK_SECONDS` (75) | before each agent node, before each Investigator turn, before each Verifier model call |
| Redrafts after an unsupported verdict | `AGENT_MAX_VERIFY_RETRIES` (1) | in the verify node |
| Model calls per Verifier pass | 2 | in code |
| Searches per Verifier pass | 3, k=5 | in code |
| Linked clauses per Verifier pass | 6 | in code |

A clean run uses at most 5 node executions (investigate, verify, investigate, verify,
finalize).

**Cost of a run** (scripted runs of S01 and S14 on the real chunks):

| Run | Model calls | Tool calls |
|---|---|---|
| verified, judge called (S01) | 4 | 14 |
| conflict found (S14, negligence draft) | 4 | 8 |
| hard rule fires (S14, unclear draft) | 2 | 8 |
| one retry | 6 to 8 | about 25 to 35 |

At `LLM_MAX_REQUESTS_PER_MINUTE=4` the gateway spaces calls 15 s apart, so the model calls,
not the tools (30 to 90 ms each), set the run time:

| Run | Model calls | Time at 4 per minute |
|---|---|---|
| hard rule fires | 2 | about 20 to 30 s |
| typical: verified or conflict | 4 | about 50 to 65 s |
| Investigator needs a third turn | 5 | about 65 to 80 s |
| one retry | 7 to 9 | about 95 to 135 s |

With the defaults (`AGENT_MAX_WALL_CLOCK_SECONDS=75`, `RUN_TIMEOUT_SECONDS=90`) a typical run
fits, but only just, and a retry cannot finish: it escalates with "investigation limit
reached". Settings that let one retry finish on the free tier, keeping
`RUN_TIMEOUT_SECONDS` under the web page's 180 s polling limit:

```
AGENT_MAX_WALL_CLOCK_SECONDS=120
AGENT_MAX_TOOL_CALLS=40
LLM_REQUEST_DEADLINE_SECONDS=40
RUN_TIMEOUT_SECONDS=175
```

`RUN_TIMEOUT_SECONDS` must exceed the wall clock plus one model call
(`LLM_REQUEST_DEADLINE_SECONDS` + 5 s), because the wall clock is checked only before a call.
Otherwise a slow last call makes the worker mark the run failed instead of escalated.

## When an agent fails mid-run (A2)

A node that fails stops the run. The graph records the error on the node's trace entry,
goes straight to finalize, and the run is **escalated**, never answered:

| Failure | In | Escalation reason |
|---|---|---|
| `LLMUnavailableError` (breaker open, retries exhausted, rate limited, gateway down) | either agent | `model unavailable (<error>)` |
| `LLMGatewayError` (auth, bad request, safety block) | either agent | `model unavailable (<error>)` |
| `ToolBudgetExceeded` | either agent | `investigation limit reached (tool-call limit of N reached)` |
| wall clock spent (between nodes or before a model call) | either agent | `investigation limit reached (wall-clock budget ...)` |
| step limit | graph | `investigation limit reached (step limit of N reached)` |
| `InvestigatorError`: malformed reply, no evidence | Investigator | its message |
| malformed Verifier reply | Verifier | handled in the Verifier: outcome `unsupported`, escalate |
| any other exception | either | propagates; the worker marks the run **failed** (no route) |

An escalated run cites only what passed the Verifier's citation check, plus the modifier
passages of a conflict. A run stopped before verification finished cites nothing. A run is
`completed` only when the Verifier returned `action="return"`.

## Trace

Each `RunResult.trace` holds, in order:
- every tool call (`search_corpus`, `get_clause`, `related`) and model call (`llm`, with
  agent, prompt version, tokens, cost, latency), from the shared wrappers;
- `investigator_draft`: fraud type, fault, route and cited ids of each draft;
- `verifier_decision`: outcome, action, reason, unsupported claims and modifier ids;
- `node`: one per node execution, with attempt, start, end, latency and outcome (and the
  error, if it stopped the run);
- `graph`: the run's totals (model calls, tool calls, steps, attempts, elapsed seconds).

`prompt_version` is `inv-<Investigator version>+ver-<Verifier version>`, e.g.
`inv-v1+ver-v1`. Prompts: `agent/prompts/v1.yaml` and `agent/prompts/verifier_v1.yaml`.
