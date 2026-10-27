"""Provider registry. LLM_PROVIDER picks the entry.

To add a provider: write providers/<name>.py with a class implementing base.Provider (map its
SDK errors onto RetryableProviderError / NonRetryableProviderError, read token counts from the
response), then add one line to PROVIDERS below and "<name>" to GatewaySettings.llm_provider.
"""

from collections.abc import Callable

from llm_gateway.providers.base import Provider
from llm_gateway.settings import GatewaySettings


def _stub(settings: GatewaySettings) -> Provider:
    from llm_gateway.providers.stub import StubProvider

    return StubProvider(settings.stub_delay_ms)


def _gemini(settings: GatewaySettings) -> Provider:
    from llm_gateway.providers.gemini import GeminiProvider

    return GeminiProvider(settings.llm_api_key)


PROVIDERS: dict[str, Callable[[GatewaySettings], Provider]] = {
    "stub": _stub,
    "gemini": _gemini,
}


def build_provider(settings: GatewaySettings) -> Provider:
    return PROVIDERS[settings.llm_provider](settings)
