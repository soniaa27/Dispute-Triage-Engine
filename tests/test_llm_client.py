"""agent.llm_client.HttpLLMClient and its use as the evaluation judge."""

import json

import fakeredis
import httpx
import pytest
from fastapi.testclient import TestClient

from agent.llm_client import CLIENT_TIMEOUT_MARGIN_S, HttpLLMClient, LLMGatewayError
from common.db import init_db, make_engine
from contracts import Citation, FraudType, Route, RunResult, RunStatus
from contracts.llm import LLMClient, LLMResponse, LLMUnavailableError
from eval.metrics import ScenarioRun, judged_metrics
from eval.schema import DEFAULT_SCENARIOS, load_scenarios
from llm_gateway.main import create_app
from llm_gateway.settings import GatewaySettings

URL = "http://gateway.test"


def ok_body(text="ok", model="test-model", step="investigator"):
    return LLMResponse(text=text, prompt_tokens=10, completion_tokens=5, cost_usd=0.0,
                       model=model, latency_ms=12.0, prompt_version="v1",
                       step=step).model_dump()


def client_with(handler, **kwargs) -> HttpLLMClient:
    return HttpLLMClient(URL, transport=httpx.MockTransport(handler), **kwargs)


def test_satisfies_llm_client_protocol():
    assert isinstance(HttpLLMClient(URL), LLMClient)


def test_success_sends_request_and_correlation_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["corr"] = request.headers["X-Correlation-ID"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=ok_body())

    client = client_with(handler, correlation_id="corr-7")
    res = client.complete("prompt text", prompt_version="v1", step="investigator",
                          run_id="run-1", max_tokens=100)
    assert isinstance(res, LLMResponse) and res.text == "ok"
    assert seen == {"path": "/v1/complete", "corr": "corr-7", "body": {
        "prompt": "prompt text", "prompt_version": "v1", "step": "investigator",
        "run_id": "run-1", "max_tokens": 100}}


def test_correlation_id_defaults_to_run_id():
    seen = {}

    def handler(request):
        seen["corr"] = request.headers["X-Correlation-ID"]
        return httpx.Response(200, json=ok_body())

    client_with(handler).complete("p", prompt_version="v1", step="s", run_id="run-9")
    assert seen["corr"] == "run-9"


def test_503_maps_to_llm_unavailable():
    def handler(request):
        return httpx.Response(503, json={"error": "llm_unavailable", "reason": "breaker_open",
                                         "breaker_state": "open"})

    with pytest.raises(LLMUnavailableError, match="breaker_open"):
        client_with(handler).complete("p", prompt_version="v1", step="s", run_id="r")


@pytest.mark.parametrize("exc", [httpx.ReadTimeout("slow"), httpx.ConnectError("refused")])
def test_timeout_or_unreachable_maps_to_llm_unavailable(exc):
    def handler(request):
        raise exc

    with pytest.raises(LLMUnavailableError):
        client_with(handler).complete("p", prompt_version="v1", step="s", run_id="r")


def test_other_errors_raise_gateway_error():
    def handler(request):
        return httpx.Response(502, json={"error": "llm_request_rejected", "reason": "auth"})

    with pytest.raises(LLMGatewayError) as info:
        client_with(handler).complete("p", prompt_version="v1", step="s", run_id="r")
    assert info.value.status_code == 502 and info.value.reason == "auth"


def test_config_from_env(monkeypatch):
    monkeypatch.delenv("LLM_GATEWAY_URL", raising=False)
    with pytest.raises(ValueError, match="LLM_GATEWAY_URL"):
        HttpLLMClient()
    monkeypatch.setenv("LLM_GATEWAY_URL", URL)
    monkeypatch.setenv("LLM_REQUEST_DEADLINE_SECONDS", "40")
    assert HttpLLMClient().timeout_s == 40 + CLIENT_TIMEOUT_MARGIN_S


def test_end_to_end_against_gateway_with_fault_injection(tmp_path):
    """Real gateway app (stub provider, fault injection on) -> 503 -> LLMUnavailableError."""
    engine = make_engine(f"sqlite:///{tmp_path / 'gw.db'}")
    init_db(engine)
    settings = GatewaySettings(redis_url="redis://unused", database_url="sqlite://",
                               llm_provider="stub", stub_delay_ms=0, llm_max_retries=0,
                               llm_fault_injection="error_500")
    gateway = TestClient(create_app(settings, redis=fakeredis.FakeAsyncRedis(), engine=engine))
    client = HttpLLMClient("http://testserver", transport=gateway._transport)
    with pytest.raises(LLMUnavailableError, match="retries_exhausted"):
        client.complete("p", prompt_version="v1", step="investigator", run_id="r")


def test_judge_through_http_client_computes_metrics_and_records_model():
    """The eval judge path: LLMJudge -> HttpLLMClient -> gateway (mocked transport here)."""
    replies = {
        "judge_claims": {"claims": [{"claim": "a", "label": "supported"},
                                    {"claim": "b", "label": "unsupported"}]},
        "judge_relevance": {"relevance": 0.5},
        "judge_faithfulness": {"faithful": True, "reason": "follows"},
    }
    steps = []

    def handler(request):
        body = json.loads(request.content)
        steps.append((body["step"], body["run_id"]))
        return httpx.Response(200, json=ok_body(json.dumps(replies[body["step"]]),
                                                model="judge-model", step=body["step"]))

    scenario = next(s for s in load_scenarios(DEFAULT_SCENARIOS) if s.id == "S01")
    result = RunResult(
        run_id="run-s01", status=RunStatus.completed, fraud_type=FraudType.skimming,
        route=Route.provisional_credit, liability_position="zero", basis="reported in time",
        citations=[Citation(doc_id="RBI_2017_CIRCULAR", clause_id="6(ii)", text="x")],
        prompt_version="test",
    )
    metrics = judged_metrics([ScenarioRun(scenario, result, 1)], client_with(handler))

    assert metrics["groundedness"] == 0.5
    assert metrics["hallucination_rate"] == 0.5
    assert metrics["context_relevance"] == 0.5
    assert metrics["faithfulness"] == 1.0
    assert metrics["judge_model"] == "judge-model"
    assert {run for _, run in steps} == {"judge-run-s01"}
