"""llm-gateway: provider mapping, retry, timeout, breaker, rate limit, cost, logging.

The google-genai client is replaced by a mock (tests only); everything else is the real code
with fakeredis (Lua enabled) and SQLite. The `live` test makes one real Gemini call and is
skipped unless run with `make llm-live-test`.
"""

import asyncio
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import fakeredis
import pytest
from fastapi.testclient import TestClient
from google.genai import errors, types
from pydantic import SecretStr
from sqlalchemy import select

from common.db import LlmCall, init_db, make_engine, make_session_factory
from llm_gateway.main import create_app
from llm_gateway.providers.gemini import GeminiProvider
from llm_gateway.providers.stub import STUB_TEXT
from llm_gateway.resilience import BreakerOpenError, BreakerState, CircuitBreaker, backoff_delay
from llm_gateway.settings import GatewaySettings

API_KEY = "AIza-TEST-SECRET-0123456789"
BODY = {"prompt": "Is the customer liable?", "prompt_version": "v1", "step": "investigator",
        "run_id": "run-123"}


def settings(**overrides) -> GatewaySettings:
    values = dict(
        redis_url="redis://unused", database_url="sqlite://", llm_provider="gemini",
        llm_model="test-model", llm_api_key=API_KEY,
        llm_price_per_1k_prompt_tokens=0.5, llm_price_per_1k_completion_tokens=1.5,
        llm_max_retries=3, llm_backoff_base_seconds=1, llm_backoff_max_seconds=10,
        llm_timeout_seconds=5, llm_request_deadline_seconds=60,
        llm_cb_failure_threshold=5, llm_cb_reset_seconds=30,
        llm_max_requests_per_minute=60000, llm_rate_limit_max_wait_seconds=1,
    )
    values.update(overrides)
    return GatewaySettings(**values)


def gemini_response(text="hello", prompt=120, candidates=30, thoughts=10):
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(role="model", parts=[types.Part(text=text)]),
            finish_reason=types.FinishReason.STOP,
        )],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt, candidates_token_count=candidates,
            thoughts_token_count=thoughts,
        ),
        model_version="test-model-001",
    )


def api_error(code: int, message: str = "error", retry_delay: str | None = None):
    status = {429: "RESOURCE_EXHAUSTED", 401: "UNAUTHENTICATED", 400: "INVALID_ARGUMENT",
              500: "INTERNAL", 503: "UNAVAILABLE"}[code]
    details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}] \
        if retry_delay else []
    body = {"error": {"code": code, "message": message, "status": status, "details": details}}
    cls = errors.ServerError if code >= 500 else errors.ClientError
    return cls(code, body)


class Harness:
    """App with a mocked google-genai client, fakeredis, SQLite and a recording sleep."""

    def __init__(self, tmp_path: Path, side_effect, **overrides):
        self.settings = settings(**overrides)
        self.sdk = MagicMock()
        self.sdk.aio.models.generate_content = AsyncMock(side_effect=side_effect)
        self.engine = make_engine(f"sqlite:///{tmp_path / 'gw.db'}")
        init_db(self.engine)
        self.redis = fakeredis.FakeAsyncRedis()
        self.delays: list[float] = []

        async def record_sleep(seconds: float) -> None:
            self.delays.append(seconds)

        provider = GeminiProvider(SecretStr(API_KEY), client=self.sdk)
        self.app = create_app(self.settings, redis=self.redis, engine=self.engine,
                              provider=provider, sleep=record_sleep)
        self.client = TestClient(self.app)

    @property
    def provider_calls(self) -> int:
        return self.sdk.aio.models.generate_content.await_count

    def rows(self) -> list[LlmCall]:
        with make_session_factory(self.engine)() as session:
            return list(session.scalars(select(LlmCall).order_by(LlmCall.id)))


# --- success, tokens, cost ----------------------------------------------------------------


def test_success_returns_tokens_cost_latency_and_writes_row(tmp_path):
    h = Harness(tmp_path, [gemini_response()])
    res = h.client.post("/v1/complete", json=BODY, headers={"X-Correlation-ID": "corr-1"})

    assert res.status_code == 200
    assert res.headers["X-Correlation-ID"] == "corr-1"
    body = res.json()
    assert body["text"] == "hello"
    assert body["prompt_tokens"] == 120
    assert body["completion_tokens"] == 40  # candidates 30 + thinking 10, both billed as output
    assert body["cost_usd"] == pytest.approx(120 / 1000 * 0.5 + 40 / 1000 * 1.5)
    assert body["model"] == "test-model-001"
    assert body["latency_ms"] >= 0
    assert (body["prompt_version"], body["step"]) == ("v1", "investigator")

    call = h.sdk.aio.models.generate_content.await_args
    assert call.kwargs["model"] == "test-model"
    assert call.kwargs["config"].temperature == 0.0

    [row] = h.rows()
    assert (row.run_id, row.correlation_id, row.step, row.prompt_version) == (
        "run-123", "corr-1", "investigator", "v1")
    assert (row.provider, row.model, row.outcome, row.attempts) == (
        "gemini", "test-model-001", "ok", 1)
    assert (row.prompt_tokens, row.completion_tokens) == (120, 40)
    assert row.cost_usd == pytest.approx(body["cost_usd"])
    assert row.error_type is None

    metrics = h.client.get("/metrics").text
    assert 'llm_requests_total{model="test-model-001",outcome="ok",provider="gemini",' \
           'step="investigator"}' in metrics
    assert "llm_tokens_total" in metrics and "llm_cost_usd_total" in metrics
    assert "llm_latency_seconds_bucket" in metrics and "llm_circuit_state 0.0" in metrics


def test_stub_provider_unchanged(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'gw.db'}")
    init_db(engine)
    app = create_app(GatewaySettings(redis_url="redis://unused", database_url="sqlite://",
                                     llm_provider="stub", stub_delay_ms=0),
                     redis=fakeredis.FakeAsyncRedis(), engine=engine)
    client = TestClient(app)
    body = client.post("/v1/complete", json=BODY).json()
    assert (body["text"], body["model"], body["cost_usd"]) == (STUB_TEXT, "stub", 0.0)
    assert body["prompt_tokens"] >= 1 and body["completion_tokens"] >= 1
    assert client.get("/healthz").json() == {"status": "ok", "provider": "stub",
                                             "fault_injection": "off"}


def test_gemini_requires_model_and_key():
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        settings(llm_api_key=" ")
    with pytest.raises(ValueError, match="LLM_MODEL"):
        settings(llm_model="")
    # Whitespace in .env values is tolerated.
    assert settings(llm_provider="gemini ").llm_provider == "gemini"


# --- retry --------------------------------------------------------------------------------


def test_retries_429_and_5xx_with_backoff(tmp_path):
    h = Harness(tmp_path, [api_error(429, "quota", retry_delay="2s"), api_error(503),
                           gemini_response()])
    res = h.client.post("/v1/complete", json=BODY)

    assert res.status_code == 200
    assert h.provider_calls == 3
    assert len(h.delays) == 2
    assert h.delays[0] >= 2.0          # the provider's RetryInfo hint is a floor
    assert 1.0 <= h.delays[1] <= 2.0   # attempt 2: ceiling base*2 = 2, equal jitter -> [1, 2]
    [row] = h.rows()
    assert (row.outcome, row.attempts) == ("retried_ok", 3)
    assert "llm_retries_total" in h.client.get("/metrics").text


@pytest.mark.parametrize("code,error_type", [(401, "auth"), (400, "bad_request")])
def test_no_retry_on_auth_or_bad_request(tmp_path, code, error_type):
    h = Harness(tmp_path, [api_error(code)] * 5)
    res = h.client.post("/v1/complete", json=BODY)

    assert res.status_code == 502
    assert res.json()["error"] == "llm_request_rejected"
    assert res.json()["reason"] == error_type
    assert h.provider_calls == 1 and h.delays == []
    [row] = h.rows()
    assert (row.outcome, row.error_type, row.attempts) == ("failed", error_type, 1)


def test_retries_exhausted_returns_503(tmp_path):
    h = Harness(tmp_path, [api_error(500)] * 10, llm_max_retries=2)
    res = h.client.post("/v1/complete", json=BODY)

    assert res.status_code == 503
    assert res.json() == {"error": "llm_unavailable", "reason": "retries_exhausted",
                          "breaker_state": "closed"}
    assert h.provider_calls == 3
    [row] = h.rows()
    assert (row.outcome, row.error_type, row.attempts) == ("failed", "server_error", 3)


def test_backoff_delay_bounds():
    upper = lambda lo, hi: hi  # noqa: E731
    lower = lambda lo, hi: lo  # noqa: E731
    assert backoff_delay(1, base=1, maximum=10, rng=upper) == 1
    assert backoff_delay(1, base=1, maximum=10, rng=lower) == 0.5
    assert backoff_delay(3, base=1, maximum=10, rng=upper) == 4
    assert backoff_delay(10, base=1, maximum=10, rng=upper) == 10   # capped
    assert backoff_delay(1, base=1, maximum=10, retry_after=7, rng=upper) == 7


# --- timeout ------------------------------------------------------------------------------


def test_timeout_is_enforced(tmp_path):
    async def hang(**_):
        await asyncio.sleep(30)

    h = Harness(tmp_path, hang, llm_timeout_seconds=0.2, llm_max_retries=0)
    started = time.monotonic()
    res = h.client.post("/v1/complete", json=BODY)

    assert time.monotonic() - started < 3
    assert res.status_code == 503 and res.json()["reason"] == "retries_exhausted"
    [row] = h.rows()
    assert (row.outcome, row.error_type) == ("failed", "timeout")


# --- circuit breaker ----------------------------------------------------------------------


def test_breaker_opens_fails_fast_half_open_trial_and_closes(tmp_path):
    outcomes = [api_error(500)] * 3 + [api_error(500)] + [gemini_response()]
    h = Harness(tmp_path, outcomes, llm_cb_failure_threshold=3, llm_cb_reset_seconds=0.3,
                llm_max_retries=0)

    for _ in range(3):
        assert h.client.post("/v1/complete", json=BODY).status_code == 503
    status = h.client.get("/v1/breaker").json()
    assert status["state"] == "open" and status["times_opened"] == 1

    # Open: fail fast, provider not called.
    res = h.client.post("/v1/complete", json=BODY)
    assert res.status_code == 503
    assert res.json() == {"error": "llm_unavailable", "reason": "breaker_open",
                          "breaker_state": "open"}
    assert h.provider_calls == 3
    assert "llm_circuit_state 2.0" in h.client.get("/metrics").text

    # Reset window passes: one half-open trial; it fails, so the breaker re-opens.
    time.sleep(0.35)
    assert h.client.post("/v1/complete", json=BODY).status_code == 503
    assert h.provider_calls == 4
    assert h.client.get("/v1/breaker").json()["state"] == "open"

    # Next trial succeeds: closed again.
    time.sleep(0.35)
    assert h.client.post("/v1/complete", json=BODY).status_code == 200
    status = h.client.get("/v1/breaker").json()
    assert status["state"] == "closed" and status["consecutive_failures"] == 0
    assert status["times_opened"] == 2 and status["fast_failures"] == 1
    assert [r.outcome for r in h.rows()] == ["failed"] * 3 + ["breaker_open", "failed", "ok"]


def test_breaker_allows_one_trial_at_a_time():
    now = [1000.0]
    breaker = CircuitBreaker(fakeredis.FakeAsyncRedis(), failure_threshold=1, reset_seconds=10,
                             trial_ttl_seconds=60, clock=lambda: now[0])

    async def scenario():
        assert await breaker.record_failure() == BreakerState.open
        with pytest.raises(BreakerOpenError):
            await breaker.before_call()
        now[0] += 10
        assert await breaker.before_call() == BreakerState.half_open
        with pytest.raises(BreakerOpenError):   # a second caller during the trial
            await breaker.before_call()
        assert await breaker.record_success() == BreakerState.closed
        assert await breaker.before_call() == BreakerState.closed

    asyncio.run(scenario())


def test_bad_request_does_not_trip_breaker(tmp_path):
    h = Harness(tmp_path, [api_error(400)] * 5, llm_cb_failure_threshold=2)
    for _ in range(3):
        h.client.post("/v1/complete", json=BODY)
    assert h.client.get("/v1/breaker").json()["state"] == "closed"


# --- rate limiting ------------------------------------------------------------------------


def test_rate_limiter_returns_rate_limited_when_exhausted(tmp_path):
    h = Harness(tmp_path, [gemini_response()] * 3, llm_max_requests_per_minute=1,
                llm_rate_limit_max_wait_seconds=1)
    assert h.client.post("/v1/complete", json=BODY).status_code == 200
    res = h.client.post("/v1/complete", json=BODY)

    assert res.status_code == 503
    assert res.json()["reason"] == "rate_limited"
    assert h.provider_calls == 1
    assert [r.outcome for r in h.rows()] == ["ok", "rate_limited"]


def test_rate_limiter_waits_within_limit(tmp_path):
    # 120 rpm -> one request every 0.5 s; a 2 s wait budget lets the second one through.
    h = Harness(tmp_path, [gemini_response()] * 2, llm_max_requests_per_minute=120,
                llm_rate_limit_max_wait_seconds=2)
    assert h.client.post("/v1/complete", json=BODY).status_code == 200
    started = time.monotonic()
    assert h.client.post("/v1/complete", json=BODY).status_code == 200
    assert 0.3 < time.monotonic() - started < 1.5
    assert h.provider_calls == 2


# --- fault injection and logging ----------------------------------------------------------


def test_fault_injection_fails_calls_and_logs_loudly(tmp_path, capsys):
    h = Harness(tmp_path, [gemini_response()], llm_fault_injection="error_429",
                llm_max_retries=1)
    res = h.client.post("/v1/complete", json=BODY)
    assert res.status_code == 503
    assert h.provider_calls == 0
    out = capsys.readouterr().out
    assert "FAULT INJECTION ENABLED" in out and "FAULT_INJECTED" in out
    assert h.client.get("/v1/breaker").json()["fault_injection"] == "error_429"


def test_api_key_and_prompt_never_logged(tmp_path, capsys):
    # The provider error message echoes the key: the log line must be redacted.
    h = Harness(tmp_path, [api_error(401, f"API key not valid: {API_KEY}")])
    secret_prompt = "Customer CUST-998877 says the OTP was 445566"
    h.client.post("/v1/complete", json={**BODY, "prompt": secret_prompt},
                  headers={"X-Correlation-ID": "corr-log"})
    out = capsys.readouterr().out

    assert API_KEY not in out
    assert "[REDACTED]" in out
    assert secret_prompt not in out          # INFO logs the length and a hash only
    assert '"prompt_sha256"' in out and '"prompt_chars": 44' in out
    for line in [line for line in out.splitlines() if '"llm_request"' in line]:
        assert '"correlation_id": "corr-log"' in line
        assert '"run_id": "run-123"' in line and '"step": "investigator"' in line


# --- live ---------------------------------------------------------------------------------


@pytest.mark.live
def test_live_gemini_call(tmp_path):
    """One real call to the configured provider, using .env. Run with `make llm-live-test`."""
    env_file = Path(__file__).resolve().parent.parent / ".env"
    live = GatewaySettings(_env_file=env_file, redis_url="redis://unused",
                           database_url="sqlite://")
    if live.llm_provider != "gemini" or os.environ.get("CI"):
        pytest.skip("needs LLM_PROVIDER=gemini and LLM_API_KEY in .env")
    engine = make_engine(f"sqlite:///{tmp_path / 'gw.db'}")
    init_db(engine)
    client = TestClient(create_app(live, redis=fakeredis.FakeAsyncRedis(), engine=engine))

    res = client.post("/v1/complete", json={
        "prompt": "Reply with exactly the word: pong", "prompt_version": "live-test",
        "step": "live_test", "run_id": "live-test"})
    assert res.status_code == 200, res.text
    body = res.json()
    print(f"\nlive call: model={body['model']} prompt_tokens={body['prompt_tokens']} "
          f"completion_tokens={body['completion_tokens']} latency_ms={body['latency_ms']} "
          f"text={body['text'].strip()[:40]!r}")
    assert body["prompt_tokens"] > 0 and body["completion_tokens"] > 0
    assert "pong" in body["text"].lower()
