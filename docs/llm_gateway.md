# LLM gateway

`llm-gateway` is the only component that talks to a language model provider. Agents and the
evaluation judge call it over HTTP through `agent.llm_client.HttpLLMClient`. It owns the
provider key, the timeout, retries, the circuit breaker, rate limiting and cost accounting
(requirements A5 and L5).

```
agent / eval judge ──HttpLLMClient──▶ POST /v1/complete
                                         │
                       breaker check ─▶ rate limiter ─▶ provider call (timeout)
                         (Redis)          (Redis)          │  retry with backoff
                                                           ▼
                                        llm_calls row (Postgres) + Prometheus metrics
```

## Providers

`LLM_PROVIDER` selects one entry of `llm_gateway/providers/__init__.py: PROVIDERS`.

| Value | File | Use |
|-------|------|-----|
| `stub` | `providers/stub.py` | Fixed response after `STUB_DELAY_MS`, approximate token counts. No key. Dev, CI, load tests. Default in `.env.example`. |
| `gemini` | `providers/gemini.py` | Google Gemini through the official `google-genai` SDK. Needs `LLM_MODEL` and `LLM_API_KEY`. |

Every provider implements `providers/base.py: Provider`:

```python
async def generate(prompt, *, model, max_tokens, temperature, timeout_s) -> ProviderResult
# ProviderResult(text, prompt_tokens, completion_tokens, model, raw_finish_reason)
```

and raises one of two errors:

| Error | Raised for | Gateway behaviour |
|-------|-----------|-------------------|
| `RetryableProviderError` | timeout, HTTP 429, 5xx, connection error | retried with backoff; counts as a breaker failure |
| `NonRetryableProviderError` | auth (401/403), bad request (400/404), safety block | not retried; HTTP 502 to the client; auth counts as a breaker failure, the others do not |

Gemini specifics:

- **Token counts** come from `usage_metadata`. `completion_tokens` = candidate tokens +
  thinking tokens, because Gemini bills thinking tokens as output.
- **SDK retries are switched off.** One `generate()` is one HTTP request, so the gateway's
  retry and breaker counts are exact.
- **Retry-After:** a 429's `google.rpc.RetryInfo.retryDelay` (or a `Retry-After` header) is
  used as the minimum backoff.

**Switching provider** is configuration only: set `LLM_PROVIDER`, `LLM_MODEL` and
`LLM_API_KEY` in `.env`, then `docker compose up -d llm-gateway`. The repository's
`.env.example` keeps `LLM_PROVIDER=stub`; each developer sets `gemini` in their own `.env`.

**Adding a provider** (for example OpenAI or a local model):

1. Write `llm_gateway/providers/<name>.py` with a class implementing `Provider`: call the SDK,
   read token counts from its usage data, and map its exceptions onto the two errors above.
2. Add one entry to `PROVIDERS` in `providers/__init__.py` and the name to the
   `llm_provider` `Literal` in `llm_gateway/settings.py`.
3. Pin its SDK in `llm_gateway/requirements.txt`. Add a mocked-SDK test like the Gemini ones
   in `tests/test_llm_gateway.py`.

Nothing outside `llm_gateway/` changes.

## Request and response

`POST /v1/complete`

| Field | Type | |
|-------|------|-|
| `prompt` | str | required |
| `prompt_version` | str | required, recorded per call |
| `step` | str | required, graph step, e.g. `investigator`, `verifier`, `judge_claims` |
| `run_id` | str | required |
| `max_tokens` | int | optional; provider default if absent |
| `temperature` | float | optional, default 0 |

Header `X-Correlation-ID` is propagated (generated if absent) and echoed back.

| Status | Body |
|--------|------|
| 200 | `contracts.llm.LLMResponse`: `text, prompt_tokens, completion_tokens, cost_usd, model, latency_ms, prompt_version, step` |
| 503 | `{"error": "llm_unavailable", "reason": "breaker_open" \| "retries_exhausted" \| "rate_limited", "breaker_state": ...}` |
| 502 | `{"error": "llm_request_rejected", "reason": "auth" \| "bad_request" \| "safety_block", "breaker_state": ...}` |
| 422 | invalid request body |

`HttpLLMClient` turns 503, a client timeout or an unreachable gateway into
`LLMUnavailableError`, and any other non-200 into `LLMGatewayError`. What the agents must do
then is in [interfaces.md](interfaces.md#when-the-model-is-unavailable).

Other endpoints: `GET /v1/breaker` (breaker state and counters), `GET /healthz`,
`GET /metrics`.

**Logging.** Every line carries `correlation_id`, `run_id`, `step` and `prompt_version`.
At INFO the prompt is logged only as `prompt_chars` and `prompt_sha256` (first 16 hex
characters); the full prompt is logged at DEBUG only. The API key is redacted from every log
line, including provider error messages.

## Resilience settings (A5)

| Variable | Default | Meaning |
|----------|---------|---------|
| `LLM_TIMEOUT_SECONDS` | 30 | Timeout for each provider call |
| `LLM_REQUEST_DEADLINE_SECONDS` | 60 | Whole-request budget: rate-limit wait + all attempts + backoff. A retry that would end past it is skipped |
| `LLM_MAX_RETRIES` | 3 | Retries after the first attempt, retryable errors only |
| `LLM_BACKOFF_BASE_SECONDS` | 1 | Backoff before retry n: `min(max, base·2^(n-1))`, half fixed and half random (equal jitter) |
| `LLM_BACKOFF_MAX_SECONDS` | 10 | Cap; a provider Retry-After hint is a floor |
| `LLM_CB_FAILURE_THRESHOLD` | 5 | Consecutive provider failures that open the breaker |
| `LLM_CB_RESET_SECONDS` | 30 | Time open before one half-open trial call |
| `LLM_MAX_REQUESTS_PER_MINUTE` | 4 | Client-side rate limit toward the provider |
| `LLM_RATE_LIMIT_MAX_WAIT_SECONDS` | 20 | Longest wait for the rate limiter before 503 `rate_limited` |
| `LLM_FAULT_INJECTION` | off | `off`, `timeout`, `error_500`, `error_429` |

Time budget chain: provider call ≤ 30 s < gateway deadline 60 s < `HttpLLMClient` timeout
65 s < `AGENT_MAX_WALL_CLOCK_SECONDS` 75 s < `RUN_TIMEOUT_SECONDS` 90 s. An agent that loses
the model still has time to escalate cleanly before the worker kills the run.

### Circuit breaker

```
                 success (resets failure count)
                ┌──────┐
                ▼      │
           ┌──────────┐   LLM_CB_FAILURE_THRESHOLD consecutive failures   ┌──────────┐
  start ──▶│  CLOSED  │ ─────────────────────────────────────────────────▶│   OPEN   │◀─┐
           └──────────┘                                                    └──────────┘  │
                ▲                                                every call:   │         │
                │                                                503 fast-fail │         │
                │ trial call succeeds                  LLM_CB_RESET_SECONDS    │         │
                │                                      elapsed, next call      ▼         │
                │                                                       ┌───────────┐    │
                └───────────────────────────────────────────────────────│ HALF-OPEN │────┘
                                                                        └───────────┘
                                                      one trial call; other calls fast-fail;
                                                      trial fails -> OPEN again
```

- **What counts as a failure:** a provider attempt that failed with timeout, 429, 5xx, a
  connection error or auth. Each retry attempt counts, so a request that retries can open the
  breaker by itself; its remaining retries then fail fast.
- **What does not count:** a bad request or a safety block. The provider answered, so it
  counts as a success for the breaker.
- **State lives in Redis** (`llm:breaker` hash, `llm:breaker:trial` lock). An open breaker
  stays open across a gateway restart, and replicas share it (D-012). Only one caller gets
  the half-open trial: the lock is `SET NX` with a TTL, so a crashed trial frees itself.
- **`GET /v1/breaker`** returns `state`, `consecutive_failures`, `failure_threshold`,
  `trial_allowed_in_seconds`, `times_opened`, `fast_failures`, `successes` and `failures`.
  `make llm-breaker` prints it.

### Rate limiting

A token bucket in Redis (`llm:ratelimit`, one atomic Lua script) shared by every gateway
replica. Capacity is 1 token, refilled at `LLM_MAX_REQUESTS_PER_MINUTE / 60` per second, so
provider requests are spaced evenly. A burst followed by a full refill could otherwise exceed
a per-minute quota. Each retry attempt takes a token too. A request waits up to
`LLM_RATE_LIMIT_MAX_WAIT_SECONDS`, then gets 503 `rate_limited` without calling the provider.

The default of 4 requests per minute stays under the lowest free-tier Flash limit published
for Gemini (5 RPM). Google shows a project's actual limits only in AI Studio
(https://aistudio.google.com/rate-limit). Raise the setting only after checking them. Daily
quotas (RPD) are not enforced by the gateway. When a daily quota runs out, Gemini answers
429 until midnight Pacific time; the breaker opens, and the agents escalate.

## Cost and token tracking (L5)

Every request, successful or not, writes one row to Postgres `llm_calls`:

`id, run_id, correlation_id, step, prompt_version, provider, model, prompt_tokens,
completion_tokens, cost_usd, latency_ms, attempts, outcome, error_type, created_at`

| Column | Values |
|--------|--------|
| `outcome` | `ok`, `retried_ok`, `failed`, `breaker_open` (no attempt made), `rate_limited` (no attempt made) |
| `error_type` | `timeout`, `server_error`, `rate_limited_by_provider`, `connection`, `auth`, `bad_request`, `safety_block`, `breaker_open`, `rate_limited` |

- **Cost** is computed at call time (D-014):
  `prompt_tokens/1000 × LLM_PRICE_PER_1K_PROMPT_TOKENS + completion_tokens/1000 × LLM_PRICE_PER_1K_COMPLETION_TOKENS`.
  Both prices are 0 on the free tier, so cost is 0 but tokens are still recorded.
- **Table creation:** the table is declared in `common/db.py` and created by the API's
  `create_all` at startup, like the other tables. The gateway starts after the API is
  healthy.
- **Failure handling:** a failed insert is logged (`llm_call_persist_failed`) and never fails
  the call.

Prometheus metrics on `GET /metrics`:

| Metric | Labels |
|--------|--------|
| `llm_requests_total` | provider, model, step, outcome |
| `llm_tokens_total` | type (`prompt`, `completion`), step |
| `llm_cost_usd_total` | step |
| `llm_latency_seconds` (histogram, whole request including retries) | step |
| `llm_retries_total` | |
| `llm_circuit_state` (0 closed, 1 half-open, 2 open) | |

**Cost report:** `make llm-cost-report` runs `scripts/cost_report.py` in the gateway container
(add `ARGS="--since-hours 24"` to limit the window). It prints:

- totals, and cost and tokens per 100 runs (distinct `run_id`s);
- per-step and per-run tables;
- a per-outcome table;
- the **largest cost driver**: the step with the highest total cost, or the highest tokens
  when every price is 0.

Judge calls are recorded under `run_id = judge-<run_id>` with steps `judge_claims`,
`judge_relevance` and `judge_faithfulness`, so evaluation cost is visible separately from
agent cost.

## Fault injection and the breaker demo

`LLM_FAULT_INJECTION` makes every provider call fail before the provider is called:

| Value | Behaviour |
|-------|-----------|
| `timeout` | the call hangs past the timeout |
| `error_500` | retryable server error |
| `error_429` | retryable rate limit with `Retry-After: 1` |

It defaults to `off`. When it is on, the gateway logs `FAULT INJECTION ENABLED` at startup and
`FAULT_INJECTED` on every call, and `/healthz` and `/v1/breaker` report it.

`make llm-breaker-demo` (stack running) uses the stub provider, so it needs no key and spends
no quota:

1. Restarts the gateway with `error_500`, a threshold of 3 and a 15 s reset window.
2. Fires requests until the breaker opens.
3. Shows a 503 fast-fail that does not reach the provider.
4. Restarts the gateway with fault injection off and shows the breaker is *still open*,
   because its state is in Redis.
5. Waits out the reset window and shows the half-open trial succeed and the breaker close.
6. Restores the gateway to the `.env` configuration.

## Live test

`make llm-live-test` runs the one test marked `live`. It loads `.env` and makes a single real
call through the full gateway path (breaker, limiter, provider, `llm_calls` row in a temporary
SQLite file). It is skipped unless `LLM_PROVIDER=gemini`, and is excluded from `make test` and
CI by `-m 'not live'` in `pyproject.toml`.
