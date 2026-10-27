"""Google Gemini through the official google-genai SDK.

The SDK's own retries are switched off: the gateway owns retry, backoff and the breaker, so
one generate() call is exactly one HTTP request to the provider.
"""

import re

import httpx
from google import genai
from google.genai import errors, types
from pydantic import SecretStr

from llm_gateway.providers.base import (
    NonRetryableProviderError,
    ProviderResult,
    RetryableProviderError,
)

_BLOCKED_FINISH = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
_DURATION = re.compile(r"^\s*([\d.]+)s\s*$")


def _retry_after(exc: errors.APIError) -> float | None:
    """Seconds from a Retry-After header, else from a google.rpc.RetryInfo detail ("17s")."""
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    header = headers.get("retry-after") if hasattr(headers, "get") else None
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    details = exc.details.get("error", {}).get("details", []) if isinstance(exc.details,
                                                                             dict) else []
    for detail in details:
        if isinstance(detail, dict) and "retryDelay" in detail:
            match = _DURATION.match(str(detail["retryDelay"]))
            if match:
                return float(match.group(1))
    return None


def map_api_error(exc: errors.APIError) -> RetryableProviderError | NonRetryableProviderError:
    code = exc.code or 0
    message = f"{code} {exc.status}: {exc.message}"
    if code == 429:
        return RetryableProviderError(message, error_type="rate_limited_by_provider",
                                      status_code=code, retry_after_s=_retry_after(exc))
    if code >= 500 or code == 408:
        return RetryableProviderError(message, error_type="server_error", status_code=code,
                                      retry_after_s=_retry_after(exc))
    if code in (401, 403):
        return NonRetryableProviderError(message, error_type="auth", status_code=code,
                                         provider_unhealthy=True)
    return NonRetryableProviderError(message, error_type="bad_request", status_code=code)


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: SecretStr, client: genai.Client | None = None):
        self._client = client or genai.Client(
            api_key=api_key.get_secret_value(),
            http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(attempts=1)),
        )

    async def generate(self, prompt, *, model, max_tokens, temperature, timeout_s):
        config = types.GenerateContentConfig(
            temperature=temperature,
            max_output_tokens=max_tokens,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )
        try:
            response = await self._client.aio.models.generate_content(
                model=model, contents=prompt, config=config
            )
        except errors.APIError as exc:
            raise map_api_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise RetryableProviderError(f"timeout: {exc}", error_type="timeout") from exc
        except httpx.TransportError as exc:
            raise RetryableProviderError(f"connection error: {exc}",
                                         error_type="connection") from exc

        feedback = response.prompt_feedback
        if feedback is not None and feedback.block_reason:
            raise NonRetryableProviderError(f"prompt blocked: {feedback.block_reason}",
                                            error_type="safety_block")
        candidate = response.candidates[0] if response.candidates else None
        finish = str(candidate.finish_reason.value) if candidate and candidate.finish_reason \
            else None
        text = response.text or ""
        if not text and finish in _BLOCKED_FINISH:
            raise NonRetryableProviderError(f"response blocked: {finish}",
                                            error_type="safety_block")

        usage = response.usage_metadata
        if usage is None:
            raise RetryableProviderError("response has no usage metadata",
                                         error_type="missing_usage")
        return ProviderResult(
            text=text,
            prompt_tokens=usage.prompt_token_count or 0,
            # Thinking tokens are billed as output, so they count as completion tokens.
            completion_tokens=(usage.candidates_token_count or 0)
            + (usage.thoughts_token_count or 0),
            model=response.model_version or model,
            raw_finish_reason=finish,
        )
