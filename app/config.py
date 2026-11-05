from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://transfer:transfer_dev@localhost:55438/transfer"
    data_dir: Path = Path("data")
    provider_url: str = "http://localhost:9000"
    callback_base_url: str = "http://api:8080"
    provider_webhook_secret: str = "local-demo-secret"
    allow_scenarios: bool = False
    max_rate_age_sec: int = 3600
    provider_timeout_sec: int = 3
    poll_interval_sec: int = 2
    lease_sec: int = 20
    review_after_sec: int = 120


@lru_cache
def settings() -> Settings:
    return Settings()
