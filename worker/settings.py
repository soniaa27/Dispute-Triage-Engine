from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    """Loaded from environment variables (see .env.example). No endpoint defaults on purpose."""

    model_config = SettingsConfigDict(extra="ignore")

    database_url: str
    redis_url: str
    queue_name: str = "disputes"
    run_timeout_seconds: int = 90
    log_level: str = "INFO"
