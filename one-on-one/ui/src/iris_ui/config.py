"""Settings for the Iris UI.

Values are loaded from environment variables (optionally from a local
``.env`` file). See ``.env.example`` for the supported keys.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration for the UI process."""

    BRAIN_URL: str = "http://localhost:8096"
    IRIS_UI_PORT: int = 8097
    BRAIN_TIMEOUT_SECONDS: float = 5.0
    # Token enviado al brain en header X-Iris-Admin-Token para /admin/*.
    IRIS_ADMIN_TOKEN: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
