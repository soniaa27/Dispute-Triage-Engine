"""Interface between the agents and the language model.

Agents depend only on LLMClient; the implementation (talking to llm-gateway, with
timeouts, retries and the circuit breaker) is swapped in by configuration.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field


class LLMResponse(BaseModel):
    """One completion, with the usage and cost recorded at call time (requirement L5)."""

    text: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_usd: float = Field(ge=0)
    model: str
    latency_ms: float = Field(ge=0)
    prompt_version: str
    step: str = Field(description="Graph step that made the call, e.g. 'investigator'.")


class LLMUnavailableError(Exception):
    """The model cannot be reached: circuit open, retries exhausted or provider down.

    Callers must not answer when they catch this: escalate or fail the run.
    """


@runtime_checkable
class LLMClient(Protocol):
    def complete(
        self,
        prompt: str,
        *,
        prompt_version: str,
        step: str,
        run_id: str,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Return one completion. Raises LLMUnavailableError if the model is unavailable."""
        ...
