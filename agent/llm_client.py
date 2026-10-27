"""HttpLLMClient: the agents' contracts.llm.LLMClient, calling llm-gateway over HTTP.

The gateway owns retries, the breaker and rate limiting; this client makes one HTTP request
per complete() and does not retry. Configuration (environment):
  LLM_GATEWAY_URL               gateway base URL (required)
  LLM_REQUEST_DEADLINE_SECONDS  the gateway's whole-request budget; this client waits
                                CLIENT_TIMEOUT_MARGIN_S longer so the gateway answers first
"""

import os

import httpx
import structlog

from contracts.llm import LLMResponse, LLMUnavailableError

CLIENT_TIMEOUT_MARGIN_S = 5.0
DEFAULT_GATEWAY_DEADLINE_S = 60.0


class LLMGatewayError(RuntimeError):
    """The gateway rejected the request (bad request, auth failure, safety block, 4xx/5xx
    other than 503). Not retryable as is; the agent should escalate, not answer."""

    def __init__(self, message: str, *, status_code: int, reason: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason


class HttpLLMClient:
    def __init__(self, base_url: str | None = None, *, timeout_s: float | None = None,
                 correlation_id: str | None = None, transport: httpx.BaseTransport | None = None):
        base_url = base_url or os.environ.get("LLM_GATEWAY_URL")
        if not base_url:
            raise ValueError("LLM_GATEWAY_URL is not set")
        if timeout_s is None:
            deadline = float(os.environ.get("LLM_REQUEST_DEADLINE_SECONDS",
                                            DEFAULT_GATEWAY_DEADLINE_S))
            timeout_s = deadline + CLIENT_TIMEOUT_MARGIN_S
        self.timeout_s = timeout_s
        self.correlation_id = correlation_id
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_s,
                                  transport=transport)

    def _correlation_id(self, run_id: str) -> str:
        # The worker binds correlation_id for the run; reuse it so gateway lines join up.
        bound = structlog.contextvars.get_contextvars().get("correlation_id")
        return self.correlation_id or bound or run_id

    def complete(self, prompt: str, *, prompt_version: str, step: str, run_id: str,
                 max_tokens: int | None = None) -> LLMResponse:
        body = {"prompt": prompt, "prompt_version": prompt_version, "step": step,
                "run_id": run_id, "max_tokens": max_tokens}
        try:
            res = self._http.post("/v1/complete", json=body,
                                  headers={"X-Correlation-ID": self._correlation_id(run_id)})
        except httpx.TimeoutException as exc:
            raise LLMUnavailableError(f"gateway did not answer within {self.timeout_s:g}s") \
                from exc
        except httpx.TransportError as exc:
            raise LLMUnavailableError(f"gateway unreachable: {exc}") from exc

        if res.status_code == 503:
            detail = _json(res)
            raise LLMUnavailableError(
                f"model unavailable: {detail.get('reason')} "
                f"(breaker {detail.get('breaker_state')})")
        if res.status_code != 200:
            detail = _json(res)
            raise LLMGatewayError(
                f"llm-gateway returned HTTP {res.status_code}: {detail or res.text[:200]}",
                status_code=res.status_code, reason=detail.get("reason"))
        return LLMResponse.model_validate(res.json())

    def close(self) -> None:
        self._http.close()


def _json(res: httpx.Response) -> dict:
    try:
        value = res.json()
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def from_env() -> HttpLLMClient:
    """Factory for `--judge-client agent.llm_client:from_env` (or `--judge-client gateway`)."""
    return HttpLLMClient()
