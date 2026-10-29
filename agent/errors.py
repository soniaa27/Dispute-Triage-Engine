"""Errors shared by the agents and whatever runs them (agent.runner, agent.graph)."""

# Escalation reasons for a run stopped before an answer (docs/interfaces.md).
MODEL_UNAVAILABLE = "model unavailable"
LIMIT_REACHED = "investigation limit reached"


class ToolBudgetExceeded(Exception):
    """The run used its shared tool-call limit (AGENT_MAX_TOOL_CALLS). Raised by the
    retriever wrapper before the call over the limit is made; the run escalates."""
