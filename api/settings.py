from pathlib import Path

from pydantic import Field
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
    # Requests per client per window on POST endpoints; 0 turns rate limiting off.
    rate_limit_per_minute: int = Field(default=10, ge=0)
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    # Key the limit on X-Client-ID instead of the IP. Self-declared, so off by default.
    rate_limit_trust_client_id: bool = False
    # How long an identical dispute reuses the first run; 0 turns the cache off.
    cache_ttl_seconds: int = Field(default=3600, ge=0)
    # Part of the cache key: runs from one agent mode are not reused by another.
    cache_namespace: str = ""
