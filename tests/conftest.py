"""Unit-test fixtures: SQLite (file in tmp_path) + fakeredis. No Docker needed.

The full stack (Postgres + Redis) is exercised by `make smoke` against docker compose.
"""

from copy import deepcopy

import fakeredis
import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.settings import ApiSettings
from common.db import init_db, make_engine

VALID_PAYLOAD = {
    "customer_id": "CUST-000123",
    "account_type": "SAVINGS",
    "channel": "CARD_NOT_PRESENT",
    "transaction_id": "TXN-2026-000001",
    "transaction_amount_inr": 4999.0,
    "transaction_datetime": "2026-09-01T14:05:00+05:30",
    "bank_alert_datetime": "2026-09-01T14:06:00+05:30",
    "reported_datetime": "2026-09-02T10:00:00+05:30",
    "home_branch_state": "Karnataka",
    "complaint_text": "An online purchase I did not make appeared on my card.",
    "customer_claims": {
        "shared_otp_or_pin": False,
        "card_in_possession": True,
        "received_alert": True,
    },
}


@pytest.fixture
def payload() -> dict:
    return deepcopy(VALID_PAYLOAD)


@pytest.fixture
def database_url(tmp_path) -> str:
    url = f"sqlite:///{tmp_path / 'test.db'}"
    init_db(make_engine(url))
    return url


@pytest.fixture
def redis():
    return fakeredis.FakeRedis()


@pytest.fixture
def engine(database_url):
    return make_engine(database_url)


@pytest.fixture
def client(database_url, redis, engine, tmp_path):
    settings = ApiSettings(
        database_url=database_url, redis_url="redis://unused", web_dir=tmp_path / "noweb"
    )
    app = create_app(settings=settings, redis=redis, engine=engine)
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def real_corpus(tmp_path_factory):
    """(data_dir, HybridRetriever) over the real chunks, embedded with the hash embedder (no
    model download). Skips when the corpus PDFs are not present."""
    from eval.corpus import corpus_available
    from ingest.build import build
    from ingest.embed import HashEmbedder
    from retrieval import HybridRetriever

    if not corpus_available():
        pytest.skip("corpus PDFs not present")
    out = tmp_path_factory.mktemp("corpus")
    build(out_dir=out, embedder=HashEmbedder())
    return out, HybridRetriever.load(out, embedder=HashEmbedder())
