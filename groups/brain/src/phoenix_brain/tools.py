"""Tools tool-use de Anthropic para Phoenix.

Cada tool tiene:
- definition: el schema que se manda en `tools=[...]`
- handler: función que recibe `(tool_input: dict, ctx: ToolContext) -> dict|str`

ctx incluye group_id (puede ser None en DM owner) y un flag is_owner.

Tools que mutan estado (update_my_soul, create_kb, subscribe_kb, etc.) son
owner-only: si el solicitante no es owner, devuelven {ok:false, reason:owner_only}.
"""
import ipaddress
import socket
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from .db import get_session
from .models import Group, GroupKb, GroupSoul, KbFact, KnowledgeBase
from .soul import invalidate_cache as invalidate_soul_cache


@dataclass
class ToolContext:
    group_id: Optional[int]
    is_owner: bool


def _subscribed_kb_ids(group_id: Optional[int]) -> list[int]:
    """KBs suscritas al grupo (vacío si DM owner)."""
    if group_id is None:
        return []
    with get_session() as s:
        rows = s.execute(
            select(GroupKb.kb_id).where(GroupKb.group_id == group_id)
        ).scalars().all()
        return list(rows)


def _resolve_group_ref(ctx: ToolContext, group_ref: Optional[str]) -> tuple[Optional[int], Optional[str]]:
    """Resuelve `group_ref` (JID exacto o substring del display_name) o cae al
    contexto actual. Devuelve (group_id, error_reason).

    - Sin group_ref + ctx.group_id None → (None, 'no_group_context')
    - Sin group_ref + ctx.group_id presente → (ctx.group_id, None)
    - group_ref con JID exacto → busca por wa_jid
    - group_ref sin '@' → fuzzy substring contra display_name (case-insensitive)
    """
    if not group_ref:
        if ctx.group_id is None:
            return None, "no_group_context"
        return ctx.group_id, None

    ref = group_ref.strip()
    with get_session() as s:
        if "@" in ref:
            g = s.execute(select(Group).where(Group.wa_jid == ref)).scalar_one_or_none()
            if not g:
                return None, f"group_not_found:{ref}"
            return g.id, None
        candidates = s.execute(
            select(Group).where(Group.is_active == True)  # noqa: E712
        ).scalars().all()
        matches = [g for g in candidates if ref.lower() in (g.display_name or "").lower()]
        if not matches:
            return None, f"no_match_for:{ref}"
        if len(matches) > 1:
            names = ", ".join(g.display_name for g in matches[:5])
            return None, f"ambiguous:{names}"
        return matches[0].id, None


# ── Tool: lookup_kb_fact ────────────────────────────────────────────
LOOKUP_KB_FACT = {
    "name": "lookup_kb_fact",
    "description": (
        "Busca un fact específico en una knowledge base por su key exacta. "
        "Úsalo cuando el usuario pregunte algo que probablemente esté en una "
        "KB suscrita (ej. precio de curso, fechas, políticas, comandos de "
        "<ECOSYSTEM>, frameworks de IA en salud). Si no encuentras el fact por "
        "key, usa list_kb_facts para ver qué hay."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kb_slug": {"type": "string", "description": "Slug de la KB (ej. 'marketing', 'ia-salud', 'openclaw')."},
            "key": {"type": "string", "description": "Clave exacta del fact a buscar."},
        },
        "required": ["kb_slug", "key"],
    },
}


def _handle_lookup_kb_fact(args: dict, ctx: ToolContext) -> dict:
    kb_slug = args.get("kb_slug", "")
    key = args.get("key", "")
    allowed_kb_ids = _subscribed_kb_ids(ctx.group_id)

    with get_session() as s:
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == kb_slug)).scalar_one_or_none()
        if kb is None:
            return {"found": False, "reason": "kb_not_found"}
        if ctx.group_id is not None and kb.id not in allowed_kb_ids:
            return {"found": False, "reason": "kb_not_subscribed_to_this_group"}
        fact = s.execute(
            select(KbFact)
            .where(KbFact.kb_id == kb.id, KbFact.key == key, KbFact.status == "active")
            .order_by(KbFact.version.desc())
        ).scalar_one_or_none()
        if fact is None:
            return {"found": False, "reason": "key_not_found"}
        return {
            "found": True,
            "kb_slug": kb_slug,
            "key": key,
            "value": fact.value,
            "source": fact.source,
            "version": fact.version,
        }


# ── Tool: list_kb_facts ─────────────────────────────────────────────
LIST_KB_FACTS = {
    "name": "list_kb_facts",
    "description": (
        "Lista las keys disponibles en una KB. Útil cuando no sabes exactamente "
        "qué key buscar y quieres explorar qué información existe. Devuelve "
        "máximo 50 keys."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kb_slug": {"type": "string"},
        },
        "required": ["kb_slug"],
    },
}


def _handle_list_kb_facts(args: dict, ctx: ToolContext) -> dict:
    kb_slug = args.get("kb_slug", "")
    allowed_kb_ids = _subscribed_kb_ids(ctx.group_id)

    with get_session() as s:
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == kb_slug)).scalar_one_or_none()
        if kb is None:
            return {"found": False, "reason": "kb_not_found"}
        if ctx.group_id is not None and kb.id not in allowed_kb_ids:
            return {"found": False, "reason": "kb_not_subscribed_to_this_group"}
        rows = s.execute(
            select(KbFact.key, KbFact.source)
            .where(KbFact.kb_id == kb.id, KbFact.status == "active")
            .order_by(KbFact.key)
            .limit(50)
        ).all()
        return {
            "found": True,
            "kb_slug": kb_slug,
            "keys": [{"key": r.key, "source": r.source} for r in rows],
        }


# ── Tool: remember_fact ─────────────────────────────────────────────
REMEMBER_FACT = {
    "name": "remember_fact",
    "description": (
        "Guarda un fact nuevo (o actualiza uno existente) en una KB. Sólo úsalo "
        "cuando el OWNER (<OWNER>) te diga explícitamente que recuerdes algo, o "
        "cuando aprendas algo claramente útil que <OWNER> debería revisar después. "
        "Si quien te lo dice NO es el owner, el fact queda en 'pending_review' "
        "para que <OWNER> lo apruebe."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kb_slug": {"type": "string"},
            "key": {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["kb_slug", "key", "value"],
    },
}


def _handle_remember_fact(args: dict, ctx: ToolContext) -> dict:
    kb_slug = args.get("kb_slug", "")
    key = args.get("key", "")
    value = args.get("value", "")
    allowed_kb_ids = _subscribed_kb_ids(ctx.group_id)

    with get_session() as s:
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == kb_slug)).scalar_one_or_none()
        if kb is None:
            return {"saved": False, "reason": "kb_not_found"}
        if ctx.group_id is not None and kb.id not in allowed_kb_ids:
            return {"saved": False, "reason": "kb_not_subscribed_to_this_group"}

        # Si existe un fact activo con misma key, lo desactivamos y creamos nueva versión.
        existing = s.execute(
            select(KbFact)
            .where(KbFact.kb_id == kb.id, KbFact.key == key, KbFact.status == "active")
            .order_by(KbFact.version.desc())
        ).scalar_one_or_none()
        next_version = (existing.version + 1) if existing else 1
        if existing:
            existing.status = "superseded"

        status = "active" if ctx.is_owner else "pending_review"
        source = "jmf" if ctx.is_owner else "auto"
        fact = KbFact(
            kb_id=kb.id,
            key=key,
            value=value,
            source=source,
            version=next_version,
            status=status,
        )
        s.add(fact)
        s.commit()
        return {
            "saved": True,
            "kb_slug": kb_slug,
            "key": key,
            "version": next_version,
            "status": status,
        }


# ── Tool: update_group_soul (owner-only) ────────────────────────────
UPDATE_MY_SOUL = {
    "name": "update_group_soul",
    "description": (
        "Reemplaza tu SOUL (system prompt) para un grupo con uno nuevo. Solo el "
        "owner (<OWNER>) puede invocarlo, y SOLO si él lo pide explícitamente. "
        "El SOUL anterior queda como versión inactiva (recuperable). Aplica a "
        "la siguiente respuesta. Por defecto edita el grupo donde estás "
        "conversando; pasa `group_ref` (nombre del grupo o JID) para editar "
        "otro grupo desde DM con el owner."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "soul_md": {
                "type": "string",
                "description": "El SOUL completo en markdown. Debe ser autocontenido.",
            },
            "group_ref": {
                "type": "string",
                "description": "Opcional. JID exacto (xxx@g.us) o substring del nombre (ej. 'archivo'). Si se omite, edita el grupo de la conversación actual.",
            },
        },
        "required": ["soul_md"],
    },
}


def _handle_update_my_soul(args: dict, ctx: ToolContext) -> dict:
    if not ctx.is_owner:
        return {"ok": False, "reason": "owner_only"}
    group_id, err = _resolve_group_ref(ctx, args.get("group_ref"))
    if err:
        return {"ok": False, "reason": err}
    soul_md = (args.get("soul_md") or "").strip()
    if not soul_md:
        return {"ok": False, "reason": "empty_soul"}
    with get_session() as s:
        g = s.get(Group, group_id)
        if not g:
            return {"ok": False, "reason": "group_not_found"}
        active = s.execute(
            select(GroupSoul)
            .where(GroupSoul.group_id == g.id, GroupSoul.is_active == True)  # noqa: E712
        ).scalar_one_or_none()
        new_version = (active.version + 1) if active else 1
        if active:
            active.is_active = False
        s.add(GroupSoul(group_id=g.id, soul_md=soul_md, version=new_version, is_active=True))
        s.commit()
        jid = g.wa_jid
        display = g.display_name
    invalidate_soul_cache(jid)
    return {"ok": True, "version": new_version, "group_jid": jid, "display_name": display}


# ── Tool: create_kb (owner-only) ────────────────────────────────────
CREATE_KB = {
    "name": "create_kb",
    "description": (
        "Crea una nueva knowledge base. Solo owner. Después de crearla puedes "
        "suscribirla al grupo actual con subscribe_kb_to_this_group, y agregar "
        "facts con remember_fact (que como owner quedan activos directo)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "kebab-case, único. Ej: 'archivo', 'oficina-rrss'."},
            "name": {"type": "string"},
            "description": {"type": "string", "description": "1-2 oraciones: qué cubre la KB y cuándo consultarla."},
        },
        "required": ["slug", "name"],
    },
}


def _handle_create_kb(args: dict, ctx: ToolContext) -> dict:
    if not ctx.is_owner:
        return {"ok": False, "reason": "owner_only"}
    slug = (args.get("slug") or "").strip()
    name = (args.get("name") or "").strip()
    description = (args.get("description") or "").strip()
    if not slug or not name:
        return {"ok": False, "reason": "slug_and_name_required"}
    with get_session() as s:
        existing = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == slug)).scalar_one_or_none()
        if existing:
            return {"ok": False, "reason": "slug_already_exists"}
        kb = KnowledgeBase(slug=slug, name=name, description=description)
        s.add(kb)
        s.commit()
        kb_id = kb.id
    return {"ok": True, "slug": slug, "id": kb_id}


# ── Tool: subscribe_kb_to_group (owner-only) ────────────────────────
SUBSCRIBE_KB = {
    "name": "subscribe_kb_to_group",
    "description": (
        "Suscribe una KB ya existente a un grupo. Solo owner. Phoenix entonces "
        "podrá lookup_kb_fact/list_kb_facts/remember_fact contra esa KB en ese "
        "grupo. priority alto = más relevancia. Por defecto suscribe al grupo "
        "actual; pasa `group_ref` (nombre o JID) desde DM owner para suscribir "
        "a otro grupo."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "kb_slug": {"type": "string"},
            "priority": {"type": "integer", "default": 0},
            "group_ref": {
                "type": "string",
                "description": "Opcional. JID o substring del nombre del grupo. Default: grupo actual.",
            },
        },
        "required": ["kb_slug"],
    },
}


def _handle_subscribe_kb(args: dict, ctx: ToolContext) -> dict:
    if not ctx.is_owner:
        return {"ok": False, "reason": "owner_only"}
    group_id, err = _resolve_group_ref(ctx, args.get("group_ref"))
    if err:
        return {"ok": False, "reason": err}
    kb_slug = (args.get("kb_slug") or "").strip()
    priority = int(args.get("priority", 0) or 0)
    with get_session() as s:
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == kb_slug)).scalar_one_or_none()
        if not kb:
            return {"ok": False, "reason": "kb_not_found"}
        existing = s.execute(
            select(GroupKb).where(GroupKb.group_id == group_id, GroupKb.kb_id == kb.id)
        ).scalar_one_or_none()
        if existing:
            existing.priority = priority
        else:
            s.add(GroupKb(group_id=group_id, kb_id=kb.id, priority=priority))
        s.commit()
    return {"ok": True, "kb_slug": kb_slug, "priority": priority, "group_id": group_id}


# ── Tool: rename_group (owner-only) ─────────────────────────────────
RENAME_GROUP = {
    "name": "rename_group",
    "description": (
        "Cambia el display_name de un grupo en la DB (cosmético; no toca el "
        "nombre real de WhatsApp). Solo owner. Por defecto renombra el grupo "
        "actual; pasa `group_ref` (nombre o JID) desde DM owner para renombrar "
        "otro grupo."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "display_name": {"type": "string"},
            "group_ref": {
                "type": "string",
                "description": "Opcional. JID o substring del nombre actual del grupo. Default: grupo actual.",
            },
        },
        "required": ["display_name"],
    },
}


def _handle_rename_group(args: dict, ctx: ToolContext) -> dict:
    if not ctx.is_owner:
        return {"ok": False, "reason": "owner_only"}
    group_id, err = _resolve_group_ref(ctx, args.get("group_ref"))
    if err:
        return {"ok": False, "reason": err}
    name = (args.get("display_name") or "").strip()
    if not name:
        return {"ok": False, "reason": "empty_name"}
    with get_session() as s:
        g = s.get(Group, group_id)
        if not g:
            return {"ok": False, "reason": "group_not_found"}
        g.display_name = name
        s.commit()
    return {"ok": True, "display_name": name, "group_id": group_id}


# ── Tool: fetch_url (no owner-only) ─────────────────────────────────
FETCH_URL = {
    "name": "fetch_url",
    "description": (
        "Descarga una URL HTTP(S) pública y devuelve su contenido principal "
        "como texto plano (HTML convertido a texto, hasta ~12K caracteres). "
        "Úsalo cuando el usuario te comparta un link y te pida opinar, "
        "resumir o extraer información. NO bajes URLs proactivamente; sólo "
        "si te lo piden o si necesitas el contexto para responder algo que "
        "te preguntaron. No accede a redes privadas (localhost, 10.x, "
        "192.168.x, 100.64-127.x Tailscale). Si una página bloquea el acceso, "
        "reintenta sola con un lector alterno; el resultado puede traer `via`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "URL completa con http:// o https://"},
        },
        "required": ["url"],
    },
}


_URL_CACHE: dict[str, tuple[dict, float]] = {}
_URL_CACHE_TTL = 300  # 5 min
_USER_AGENT = "Mozilla/5.0 (compatible; PhoenixBot/1.0)"
_FETCH_TIMEOUT = 10.0
_TEXT_TRUNCATE = 12000
_SUPPORTED_CT = ("text/html", "text/plain", "text/markdown", "application/json", "application/xhtml")
# Fallback de lectura: si el fetch directo falla/bloquea (403, paywall, JS, etc.),
# reintentamos vía Jina Reader, que devuelve el contenido en texto/markdown limpio.
_JINA_PREFIX = "https://r.jina.ai/"
_MIN_USABLE_TEXT = 200  # menos que esto ≈ página bloqueada/vacía → vale la pena el fallback


def _is_private_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # ante la duda, bloquear
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        return True
    if ip.is_private:
        return True
    # Tailscale CGNAT: <tailscale-ip>/10
    if isinstance(ip, ipaddress.IPv4Address):
        if int(ip) >> 22 == (100 << 24 | 64 << 16) >> 22:
            return True
        # Más simple y explícito:
        if 100 <= int(str(ip).split(".")[0]) and str(ip).startswith(("100.64.", "100.65.", "100.66.", "100.67.",
                                                                     "100.68.", "100.69.", "100.7", "100.8", "100.9",
                                                                     "100.10", "100.11", "100.12")):
            # cubre 100.64.x – 100.127.x
            first_octet = int(str(ip).split(".")[1])
            if 64 <= first_octet <= 127:
                return True
    return False


def _strip_html(html: str) -> tuple[str, Optional[str]]:
    """Quita scripts/styles/nav/footer y devuelve (texto, title)."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return html[:_TEXT_TRUNCATE], None
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.string.strip() if soup.title and soup.title.string else None
    for tag in soup(["script", "style", "nav", "footer", "noscript", "iframe", "svg"]):
        tag.decompose()
    # Preferir <main> o <article> si existen
    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = main.get_text(separator=" ", strip=True)
    # Colapsar espacios múltiples
    import re
    text = re.sub(r"\s+", " ", text).strip()
    return text, title


_BLOCK_MARKERS = (
    "just a moment", "captcha", "attention required", "enable javascript",
    "requiring captcha", "verify you are human", "verifying you are human",
    "cloudflare", "ddos protection", "access denied",
)


def _looks_blocked(text: str) -> bool:
    """Heurística: ¿el texto extraído es en realidad una página de bloqueo (CAPTCHA/JS)
    o está vacío? Un artículo largo que sólo mencione 'captcha' NO cuenta como bloqueo."""
    t = (text or "").strip().lower()
    if len(t) < _MIN_USABLE_TEXT:
        return True
    if len(t) < 1500 and any(m in t for m in _BLOCK_MARKERS):
        return True
    return False


def _fetch_extract(fetch_target: str, canonical_url: str) -> dict:
    """Descarga `fetch_target` y extrae texto. `canonical_url` es la URL que se
    reporta al usuario (la original, aunque se haya bajado vía un proxy/reader)."""
    try:
        r = httpx.get(
            fetch_target,
            timeout=_FETCH_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml,text/plain,*/*;q=0.5"},
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": "fetch_failed", "error": str(e)[:200]}

    if r.status_code >= 400:
        return {"ok": False, "reason": "http_error", "status_code": r.status_code}

    ct = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if not any(ct.startswith(s) for s in _SUPPORTED_CT):
        return {"ok": False, "reason": "unsupported_content_type", "content_type": ct, "status_code": r.status_code}

    body = r.text or ""
    title = None
    if ct.startswith(("text/html", "application/xhtml")):
        text, title = _strip_html(body)
    else:
        text = body

    truncated = len(text) > _TEXT_TRUNCATE
    if truncated:
        text = text[:_TEXT_TRUNCATE] + " … [truncado]"

    return {
        "ok": True,
        "url": canonical_url,
        "final_url": str(r.url),
        "title": title,
        "text": text,
        "status_code": r.status_code,
        "truncated": truncated,
        "content_type": ct,
    }


def _handle_fetch_url(args: dict, ctx: ToolContext) -> dict:
    url = (args.get("url") or "").strip()
    if not url:
        return {"ok": False, "reason": "empty_url"}

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return {"ok": False, "reason": "unsupported_scheme", "scheme": parsed.scheme}
    if not parsed.netloc:
        return {"ok": False, "reason": "missing_host"}

    # SSRF guard: resolver DNS y bloquear privadas
    host = parsed.hostname or ""
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return {"ok": False, "reason": "dns_failure", "error": str(e)}
    for info in infos:
        ip_str = info[4][0]
        if _is_private_ip(ip_str):
            return {"ok": False, "reason": "private_ip_blocked", "host": host, "resolved_ip": ip_str}

    # Cache
    now = time.time()
    cached = _URL_CACHE.get(url)
    if cached and cached[1] > now:
        return cached[0]

    # Intento 1: fetch directo.
    res = _fetch_extract(url, url)

    # Intento 2 (sólo si el primero no sirvió): Jina Reader. La URL original ya pasó el
    # guard SSRF arriba; r.jina.ai es público. Bypassea bloqueos (403/paywall/JS).
    if (not res.get("ok")) or _looks_blocked(res.get("text", "")):
        jina_res = _fetch_extract(_JINA_PREFIX + url, url)
        if jina_res.get("ok") and not _looks_blocked(jina_res.get("text", "")):
            jina_res["via"] = "jina_reader"
            res = jina_res

    # Honestidad: si lo que quedó "ok" sigue siendo una página-bloqueo (CAPTCHA/JS),
    # no la relayes como contenido — repórtala como ilegible.
    if res.get("ok") and _looks_blocked(res.get("text", "")):
        res = {
            "ok": False,
            "reason": "blocked_unreadable",
            "url": url,
            "status_code": res.get("status_code"),
            "note": "La página exige CAPTCHA/JS; no se pudo leer ni directo ni vía lector.",
        }

    _URL_CACHE[url] = (res, now + _URL_CACHE_TTL)
    return res


# ── Registry ────────────────────────────────────────────────────────
TOOL_DEFINITIONS = [
    LOOKUP_KB_FACT,
    LIST_KB_FACTS,
    REMEMBER_FACT,
    FETCH_URL,
    UPDATE_MY_SOUL,
    CREATE_KB,
    SUBSCRIBE_KB,
    RENAME_GROUP,
]

_HANDLERS: dict[str, Callable[[dict, ToolContext], Any]] = {
    "lookup_kb_fact": _handle_lookup_kb_fact,
    "list_kb_facts": _handle_list_kb_facts,
    "remember_fact": _handle_remember_fact,
    "fetch_url": _handle_fetch_url,
    "update_group_soul": _handle_update_my_soul,
    "create_kb": _handle_create_kb,
    "subscribe_kb_to_group": _handle_subscribe_kb,
    "rename_group": _handle_rename_group,
    # Aliases backward-compat con primera tanda de nombres:
    "update_my_soul_for_this_group": _handle_update_my_soul,
    "subscribe_kb_to_this_group": _handle_subscribe_kb,
    "rename_this_group": _handle_rename_group,
}


def dispatch(name: str, args: dict, ctx: ToolContext) -> Any:
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"unknown_tool: {name}"}
    try:
        return handler(args, ctx)
    except Exception as e:  # noqa: BLE001
        return {"error": "tool_handler_exception", "message": str(e)}


def subscribed_kbs_summary(group_id: Optional[int]) -> Optional[str]:
    """Genera un bloque markdown con las KBs suscritas al grupo (para el system).
    Devuelve None si no hay KBs suscritas."""
    if group_id is None:
        return None
    with get_session() as s:
        rows = s.execute(
            select(KnowledgeBase, GroupKb.priority)
            .join(GroupKb, GroupKb.kb_id == KnowledgeBase.id)
            .where(GroupKb.group_id == group_id)
            .order_by(GroupKb.priority.desc(), KnowledgeBase.slug)
        ).all()
        if not rows:
            return None
        lines = ["## Knowledge bases disponibles en este grupo", ""]
        for kb, prio in rows:
            desc = kb.description or "(sin descripción)"
            lines.append(f"- **{kb.slug}** — {kb.name}. {desc}")
        lines.append("")
        lines.append(
            "Usa `lookup_kb_fact` cuando creas que la respuesta está cacheada en "
            "alguna de estas KBs. Si no estás seguro de la key exacta, usa "
            "`list_kb_facts` primero. Usa `remember_fact` sólo si el owner te lo "
            "pide o si aprendiste algo claramente reutilizable."
        )
        return "\n".join(lines)
