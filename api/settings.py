from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parent.parent


class ApiSettings(BaseSettings):
    """Loaded from environment variables (see .env.example). No endpoint defaults on purpose."""

    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    redis_url: str
    queue_name: str = "disputes"
    run_timeout_seconds: int = 90
    log_level: str = "INFO"
    web_dir: Path = _REPO_ROOT / "web"
