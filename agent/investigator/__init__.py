"""Dispute Investigator agent (Person 1): drafts fraud type, fault, delay, route and
position from retrieved corpus passages, for the Verifier to check."""

from agent.investigator.loop import investigate
from agent.investigator.prompt import InvestigatorError, ReplyFormatError

__all__ = ["InvestigatorError", "ReplyFormatError", "investigate"]
