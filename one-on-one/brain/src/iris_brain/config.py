"""Configuración global. Lee env vars con pydantic-settings."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Modelos ---
    # Default barato: Haiku 4.5 para intents + respuestas directas.
    IRIS_BRAIN_MODEL_DEFAULT: str = "claude-haiku-4-5-20251001"
    # Safety paths: Sonnet 4.6 para crisis y razonamiento clínico.
    IRIS_BRAIN_MODEL_SAFETY: str = "claude-sonnet-4-6"
    IRIS_BRAIN_MAX_TOKENS: int = 1024
    IRIS_BRAIN_THINKING: str = "disabled"  # "adaptive" | "disabled"
    IRIS_BRAIN_EFFORT: str = ""             # "" | "low" | "medium" | "high" | "max"
    IRIS_BRAIN_MAX_HISTORY: int = 30

    # --- HTTP ---
    IRIS_BRAIN_HOST: str = "0.0.0.0"
    IRIS_BRAIN_PORT: int = 8096

    # --- Postgres ---
    IRIS_BRAIN_DB_URL: str = "postgresql+psycopg://iris:iris@localhost:5432/iris"

    # --- SOUL ---
    IRIS_BRAIN_SOUL_PATH: Path = Path("SOUL.md")

    # --- Anthropic ---
    ANTHROPIC_API_KEY: str = ""

    # --- Relays ---
    # Sprint 2 frozen decision: tickets a OWNER van por Telegram bot dedicado.
    # JMF_RELAY_WEBHOOK = relay-bot Telegram (brain → OWNER).
    JMF_RELAY_WEBHOOK: str | None = "http://localhost:8098/send-to-jmf"
    # CONTACT_RELAY_WEBHOOK = wa-listener (brain → paciente vía WhatsApp).
    CONTACT_RELAY_WEBHOOK: str | None = "http://localhost:8099/send-to-contact"

    # --- Admin auth (Sprint 3) ---
    # Token compartido entre brain y UI para /admin/*. Si vacío, se genera
    # uno aleatorio al primer arranque y se persiste en runtime_config.
    IRIS_ADMIN_TOKEN: str = ""

    # --- Iris agéntica (Phase 1) ---
    # Para report_to_owner: brain manda Telegram directo (no via relay-bot HTTP).
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""


settings = Settings()


# ---------------------------------------------------------------------------
# Runtime settings: overrides desde tabla runtime_config (admin UI)
# ---------------------------------------------------------------------------

# Settings sobreescribibles vía /admin/config. Mapeo key DB → atributo en Settings.
OVERRIDABLE_KEYS: dict[str, str] = {
    "model_default": "IRIS_BRAIN_MODEL_DEFAULT",
    "model_safety": "IRIS_BRAIN_MODEL_SAFETY",
    "max_tokens": "IRIS_BRAIN_MAX_TOKENS",
    "thinking": "IRIS_BRAIN_THINKING",
    "effort": "IRIS_BRAIN_EFFORT",
    "prompt_caching_enabled": "IRIS_BRAIN_PROMPT_CACHING_ENABLED",
}

KNOWN_MODELS: list[str] = [
    "claude-haiku-4-5-20251001",
    "claude-sonnet-4-6",
    "claude-opus-4-7",
]

# Cache en memoria de overrides (recargado en boot y /admin/reload-config).
_overrides: dict[str, str] = {}


def _coerce(attr_name: str, raw: str):
    """Cast string desde DB al tipo declarado en Settings."""
    # int
    if attr_name in {"IRIS_BRAIN_MAX_TOKENS"}:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None
    # bool-as-string normalizado (lo dejamos como 'true'/'false')
    return raw


def load_overrides() -> None:
    """Lee runtime_config y aplica overrides en memoria al objeto `settings`.

    Tolerante: si la tabla aún no existe (pre-migración) no falla.
    """
    global _overrides
    try:
        from sqlalchemy import select

        from .db import get_session
        from .models import RuntimeConfig

        with get_session() as s:
            rows = list(s.scalars(select(RuntimeConfig)))
            _overrides = {r.key: r.value for r in rows}
    except Exception:
        _overrides = {}
        return

    for db_key, attr in OVERRIDABLE_KEYS.items():
        if db_key in _overrides:
            val = _coerce(attr, _overrides[db_key])
            if val is not None:
                try:
                    object.__setattr__(settings, attr, val)
                except Exception:
                    pass


def get_setting(db_key: str, default=None):
    """Lookup: runtime_config (en memoria) → Settings → default."""
    if db_key in _overrides:
        attr = OVERRIDABLE_KEYS.get(db_key)
        if attr:
            return getattr(settings, attr, _overrides[db_key])
        return _overrides[db_key]
    attr = OVERRIDABLE_KEYS.get(db_key)
    if attr:
        return getattr(settings, attr, default)
    return default


def override_source(db_key: str) -> str:
    """'db' si hay override persistido, 'env' si no."""
    return "db" if db_key in _overrides else "env"
