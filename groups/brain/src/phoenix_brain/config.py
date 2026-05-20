from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=[".env", "../.env"],
        env_prefix="PHOENIX_BRAIN_",
        extra="ignore",
    )

    model_default: str = "claude-haiku-4-5-20251001"
    model_safety: str = "claude-sonnet-4-6"
    model_proactive: str = "claude-haiku-4-5-20251001"
    db_url: str = "sqlite:///./phoenix.db"
    port: int = 8102
    history_window: int = 30
    proactive_threshold: float = 0.6
    proactive_cooldown_min: int = 10
    log_level: str = "info"


class TopLevelSettings(BaseSettings):
    """Vars sin prefijo PHOENIX_BRAIN_."""
    model_config = SettingsConfigDict(env_file=[".env", "../.env"], extra="ignore")

    anthropic_api_key: str = ""
    phoenix_owner_jid: str = ""
    phoenix_listener_url: str = "http://localhost:8100"


settings = Settings()
top = TopLevelSettings()
ROOT = Path(__file__).resolve().parents[3]
