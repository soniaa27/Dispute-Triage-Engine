"""Errors shared by the agents and whatever runs them (agent.runner now, the graph later)."""


class ToolBudgetExceeded(Exception):
    """The run used its shared tool-call limit (AGENT_MAX_TOOL_CALLS). Raised by the
    retriever wrapper before the call over the limit is made; the run escalates."""
