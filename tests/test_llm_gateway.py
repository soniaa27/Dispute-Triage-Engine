from fastapi.testclient import TestClient

from llm_gateway.main import create_app
from llm_gateway.settings import GatewaySettings


def test_stub_returns_fixed_response():
    client = TestClient(create_app(GatewaySettings(llm_mode="stub", stub_delay_ms=0)))
    res = client.post("/v1/complete", json={"prompt": "hello"})
    assert res.status_code == 200
    body = res.json()
    assert body["mode"] == "stub"
    assert body["prompt_tokens"] >= 1
    assert client.get("/healthz").json()["mode"] == "stub"


def test_provider_mode_not_implemented():
    client = TestClient(create_app(GatewaySettings(llm_mode="provider", stub_delay_ms=0)))
    assert client.post("/v1/complete", json={"prompt": "hello"}).status_code == 501
