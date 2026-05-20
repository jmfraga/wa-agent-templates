"""Environment configuration for iris-relay (Pydantic Settings)."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    brain_url: str = Field(default="http://localhost:8096", alias="BRAIN_URL")
    relay_bot_port: int = Field(default=8098, alias="RELAY_BOT_PORT")
    state_db_path: str = Field(default="./data/state.db", alias="STATE_DB_PATH")
    telegram_poll_interval: float = Field(default=2.0, alias="TELEGRAM_POLL_INTERVAL")
    http_timeout: float = Field(default=15.0, alias="HTTP_TIMEOUT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")
    iris_admin_token: str = Field(default="", alias="IRIS_ADMIN_TOKEN")
    ui_url: str = Field(default="http://<your-dev-host>:8097", alias="UI_URL")

    @property
    def state_db_url(self) -> str:
        p = Path(self.state_db_path)
        if not p.is_absolute():
            # Resolve relative to project root (cwd at startup is relay-bot/)
            p = Path.cwd() / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{p}"

    @property
    def telegram_api_base(self) -> str:
        return f"https://api.telegram.org/bot{self.telegram_bot_token}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
