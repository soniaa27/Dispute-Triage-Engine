"""The gateway's stub provider drives the real graph (load tests, docs/load_test.md): the
Investigator and the Verifier get replies they parse, runs reach the Verifier, clear-cut
scenarios complete and must-escalate ones escalate. The real retriever runs over the real
chunks; no model is called."""

import asyncio
import json

import pytest
import structlog

from agent.graph import GraphSettings, run_graph
from contracts import RunStatus
from contracts.llm import LLMResponse
from llm_gateway.providers.stub import STUB_TEXT, AgentReplies, StubProvider
from tests.agent_doubles import scenario_request, scenarios


class StubLLM:
    """The stub provider behind the agents' LLMClient interface, binding step and
    prompt_version as the gateway's /v1/complete handler does."""

    def __init__(self):
        self.provider = StubProvider(0, AgentReplies.load())
        self.calls: list[tuple[str, str]] = []

    def complete(self, prompt, *, prompt_version, step, run_id, max_tokens=None):
        self.calls.append((step, prompt_version))
        with structlog.contextvars.bound_contextvars(step=step, prompt_version=prompt_version):
            r = asyncio.run(self.provider.generate(prompt, model="", max_tokens=None,
                                                   temperature=0.0, timeout_s=5))
        return LLMResponse(text=r.text, prompt_tokens=r.prompt_tokens,
                           completion_tokens=r.completion_tokens, cost_usd=0.0, model=r.model,
                           latency_ms=0, prompt_version=prompt_version, step=step)


def _run(real_corpus, scenario_id):
    llm = StubLLM()
    result = run_graph(scenario_request(scenario_id), f"run-{scenario_id}", real_corpus[1], llm,
                       settings=GraphSettings(max_verify_retries=1))
    return result, llm


def test_clear_cut_run_reaches_the_verifier_and_completes(real_corpus):
    result, llm = _run(real_corpus, "S01")
    assert result.status == RunStatus.completed and result.route == "provisional_credit"
    assert [step for step, _ in llm.calls] == ["investigator", "investigator", "verifier",
                                               "verifier"]
    (decision,) = [t for t in result.trace if t["step"] == "verifier_decision"]
    assert (decision["outcome"], decision["action"]) == ("verified", "return")
    assert {f"{c.doc_id}:{c.clause_id}" for c in result.citations} == {
        "RBI_2017_CIRCULAR:6(ii)", "IDBI_POLICY:Table-I.01", "IDBI_POLICY:5.20"}


@pytest.mark.parametrize("scenario_id", sorted(scenarios()))
def test_every_scenario_finishes_as_expected(real_corpus, scenario_id):
    expected = scenarios()[scenario_id]["expected"]
    result, llm = _run(real_corpus, scenario_id)
    want = RunStatus.escalated if expected["must_escalate"] else RunStatus.completed
    assert result.status == want, result.escalation_reason
    if not expected["must_escalate"]:
        assert result.route == expected["route"]
        assert "verifier" in [step for step, _ in llm.calls]


def test_replies_are_deterministic_and_unknown_prompts_get_the_fixed_text():
    replies = AgentReplies.load()
    provider = StubProvider(0, replies)

    def generate(prompt, step, version):
        with structlog.contextvars.bound_contextvars(step=step, prompt_version=version):
            return asyncio.run(provider.generate(prompt, model="", max_tokens=None,
                                                 temperature=0.0, timeout_s=5)).text

    assert generate("Is the customer liable?", "investigator", "v1") == STUB_TEXT
    assert generate("anything", "judge", "judge-v1") == STUB_TEXT
    first = "This is turn 1 of 3.\n- Amount: Rs 20,000.00\n" \
            "- Reported to the bank: 2026-09-09T10:15:00+05:30\nRETRIEVED PASSAGES\nnone yet\n"
    reply = generate(first, "investigator", "v1")
    assert reply == generate(first, "investigator", "v1")
    assert json.loads(reply)["lookups"][0] == {"doc_id": "RBI_2017_CIRCULAR",
                                               "clause_id": "6(ii)"}
    assert generate(first, "investigator", "v9-experimental") == STUB_TEXT
