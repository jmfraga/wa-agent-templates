from anthropic import Anthropic

from .config import settings, top

_client: Anthropic | None = None


def get_client() -> Anthropic:
    global _client
    if _client is None:
        if not top.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY no configurada en .env")
        _client = Anthropic(api_key=top.anthropic_api_key)
    return _client


def model_default() -> str:
    return settings.model_default


def model_safety() -> str:
    return settings.model_safety
