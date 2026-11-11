"""Guardrails around the agents (requirement L4): input checks on the complaint text and an
output filter on every RunResult. Pure Python, no model calls, so they cost nothing and run
the same way in the API, the worker and the evaluation.

- injection.py: finds instruction-like text in the complaint and removes it before any
  agent sees the complaint.
- toxicity.py: abusive, threatening or self-harm language, on input and output.
- output.py: the determinations the service may not make (an action taken, a final or
  binding decision, an accusation of fraud) and toxic output text.
- pipeline.py: guard_input / guard_output, called by agent.runner.run_agent.
"""

from guardrails.injection import InjectionCheck, detect_injection
from guardrails.output import OutputViolation, check_output
from guardrails.pipeline import GUARDRAIL_STEP, GuardedInput, guard_input, guard_output
from guardrails.toxicity import ToxicityCheck, check_toxicity

__all__ = [
    "GUARDRAIL_STEP",
    "GuardedInput",
    "InjectionCheck",
    "OutputViolation",
    "ToxicityCheck",
    "check_output",
    "check_toxicity",
    "detect_injection",
    "guard_input",
    "guard_output",
]
