"""Verifier agent (Person 2): checks the Investigator's draft against retrieved passages and
decides whether the run returns it, retries the Investigator, or escalates."""

from agent.verifier.prompt import CURRENT_VERIFIER_PROMPT_VERSION
from agent.verifier.verify import Verification, VerifierError, verify, verify_detailed

__all__ = ["CURRENT_VERIFIER_PROMPT_VERSION", "Verification", "VerifierError", "verify",
           "verify_detailed"]
