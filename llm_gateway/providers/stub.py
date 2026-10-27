"""Stub provider: fixed response after STUB_DELAY_MS. For development and load tests (Locust),
so the system can be exercised without a key, quota or cost. Unchanged from the original stub
mode; only moved behind the Provider interface."""

import asyncio

from llm_gateway.providers.base import ProviderResult

STUB_TEXT = '{"stub": true, "note": "fixed response from llm-gateway stub mode"}'
STUB_MODEL = "stub"


def _approx_tokens(text: str) -> int:
    # Rough 4-chars-per-token estimate. Only the stub estimates; real providers report usage.
    return max(1, len(text) // 4)


class StubProvider:
    name = "stub"

    def __init__(self, delay_ms: int):
        self.delay_ms = delay_ms

    async def generate(self, prompt, *, model, max_tokens, temperature, timeout_s):
        await asyncio.sleep(self.delay_ms / 1000)
        return ProviderResult(
            text=STUB_TEXT,
            prompt_tokens=_approx_tokens(prompt),
            completion_tokens=_approx_tokens(STUB_TEXT),
            model=STUB_MODEL,
            raw_finish_reason="STOP",
        )
