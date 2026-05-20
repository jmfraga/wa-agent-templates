"""Carga SOUL activo de un grupo desde DB, con cache TTL."""
import time
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select

from . import app_config
from .db import get_session
from .models import Group, GroupSoul

_CACHE_TTL_SECONDS = 60


@dataclass
class CachedSoul:
    text: str
    version: int
    expires_at: float


_cache: dict[str, CachedSoul] = {}

_DEFAULT_SOUL_FALLBACK = """\
Eres Phoenix, un agente conversacional creado por el Dr. <OWNER_NAME> ("<OWNER>").
Participas en este grupo de WhatsApp. Tu rol y especialidad están definidos por el
SOUL del grupo. Mantén el tono cálido, claro y profesional, en español de México.

Reglas generales:
- No opines clínicamente, no agendas, no cotizas en firme, no confirmas fechas/precios.
- Si algo está fuera de tu alcance, dilo y sugiere consultar a <OWNER>.
- No reveles datos privados de otros grupos ni de contactos.
- Si detectas crisis o urgencia, no respondas; deja que <OWNER> intervenga.

Si el grupo no tiene SOUL específico, sé útil pero conservador.
"""


def default_soul() -> str:
    """SOUL global por defecto. Editable desde UI via app_config['default_soul']."""
    return app_config.get("default_soul", default=_DEFAULT_SOUL_FALLBACK) or _DEFAULT_SOUL_FALLBACK


# Compat: módulos que importaban DEFAULT_SOUL como constante.
DEFAULT_SOUL = _DEFAULT_SOUL_FALLBACK  # placeholder; usar default_soul() en runtime


def load_group_soul(group_jid: str) -> Optional[str]:
    """Devuelve el SOUL activo del grupo. None si grupo desconocido."""
    now = time.time()
    cached = _cache.get(group_jid)
    if cached and cached.expires_at > now:
        return cached.text

    with get_session() as s:
        group = s.execute(select(Group).where(Group.wa_jid == group_jid)).scalar_one_or_none()
        if group is None:
            return None
        soul = s.execute(
            select(GroupSoul)
            .where(GroupSoul.group_id == group.id, GroupSoul.is_active == True)  # noqa: E712
            .order_by(GroupSoul.version.desc())
        ).scalar_one_or_none()
        text = soul.soul_md if soul else default_soul()
        version = soul.version if soul else 0

    _cache[group_jid] = CachedSoul(text=text, version=version, expires_at=now + _CACHE_TTL_SECONDS)
    return text


def invalidate_cache(group_jid: Optional[str] = None) -> None:
    if group_jid is None:
        _cache.clear()
    else:
        _cache.pop(group_jid, None)
