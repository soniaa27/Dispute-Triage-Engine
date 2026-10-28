"""The web client: served by the API, and its enum values match the contracts."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from api.settings import ApiSettings
from contracts import AccountType, Channel, FraudType, Route, RunStatus
from contracts.agent import DocId

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
APP_JS = (WEB_DIR / "app.js").read_text(encoding="utf-8")


@pytest.fixture
def web_client(database_url, redis, engine):
    settings = ApiSettings(database_url=database_url, redis_url="redis://unused", web_dir=WEB_DIR)
    with TestClient(create_app(settings=settings, redis=redis, engine=engine)) as c:
        yield c


@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/", "text/html"),
        ("/styles.css", "text/css"),
        ("/app.js", "javascript"),
    ],
)
def test_page_and_assets_are_served(web_client, path, content_type):
    res = web_client.get(path)
    assert res.status_code == 200
    assert content_type in res.headers["content-type"]


def test_page_links_only_local_assets(web_client):
    html = web_client.get("/").text
    assert 'href="styles.css?v=' in html and 'src="app.js?v=' in html
    assert "http://" not in html and "https://" not in html


def test_api_routes_still_win_over_the_static_mount(web_client):
    assert web_client.get("/healthz").json() == {"status": "ok"}


@pytest.mark.parametrize("enum", [AccountType, Channel, FraudType, Route, RunStatus, DocId])
def test_client_enum_values_match_the_contracts(enum):
    missing = [m.value for m in enum
               if f'"{m.value}"' not in APP_JS and f"{m.value}:" not in APP_JS]
    assert not missing, f"web/app.js is missing {enum.__name__} values {missing}"
