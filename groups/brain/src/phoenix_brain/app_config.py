"""Config persistente en DB. Cachea 30s para evitar hit por chat.

Política de capas:
  1. app_config (DB) si la key existe.
  2. fallback: parámetro `default` que pasa el caller (típicamente settings.*).
  3. None si no hay default.

Setear con `set(key, value)` invalida cache y, para keys conocidas, dispara
side-effects (reset del cliente Anthropic, invalidate SOUL cache, etc.).
"""
import time
from typing import Optional

from sqlalchemy import delete, select

from .db import get_session
from .models import AppConfig

_CACHE_TTL = 30  # seg
_cache: dict[str, tuple[Optional[str], float]] = {}

# Keys conocidas (whitelist para evitar pollution).
KNOWN_KEYS = {
    "owner_jid",
    "default_soul",
    "proactive_threshold",
    "proactive_cooldown_min",
    # api key / models siguen en env por ahora — no se exponen aquí.
}


def get(key: str, default: Optional[str] = None) -> Optional[str]:
    now = time.time()
    cached = _cache.get(key)
    if cached and cached[1] > now:
        return cached[0] if cached[0] is not None else default
    with get_session() as s:
        row = s.execute(select(AppConfig).where(AppConfig.key == key)).scalar_one_or_none()
        value = row.value if row else None
    _cache[key] = (value, now + _CACHE_TTL)
    return value if value is not None else default


def get_int(key: str, default: int) -> int:
    v = get(key)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def get_float(key: str, default: float) -> float:
    v = get(key)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def set(key: str, value: Optional[str]) -> None:
    """Si value es None o '', borra la entrada (cae al default)."""
    if key not in KNOWN_KEYS:
        raise ValueError(f"unknown config key: {key}")
    with get_session() as s:
        if value is None or value == "":
            s.execute(delete(AppConfig).where(AppConfig.key == key))
        else:
            existing = s.execute(select(AppConfig).where(AppConfig.key == key)).scalar_one_or_none()
            if existing:
                existing.value = value
            else:
                s.add(AppConfig(key=key, value=value))
        s.commit()
    _cache.pop(key, None)
    _fire_side_effects(key)


def invalidate_cache(key: Optional[str] = None) -> None:
    if key is None:
        _cache.clear()
    else:
        _cache.pop(key, None)


def _fire_side_effects(key: str) -> None:
    if key == "default_soul":
        # Invalida cache de SOULs por grupo también (DEFAULT_SOUL puede ser fallback).
        try:
            from . import soul
            soul.invalidate_cache()
        except Exception:
            pass


def all_config() -> dict[str, Optional[str]]:
    """Devuelve todas las keys conocidas con su valor actual (o None si no seteada)."""
    with get_session() as s:
        rows = s.execute(select(AppConfig)).scalars().all()
        db = {r.key: r.value for r in rows}
    return {k: db.get(k) for k in KNOWN_KEYS}
