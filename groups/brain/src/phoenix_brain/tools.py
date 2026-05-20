"""Tools tool-use de Anthropic para Phoenix.

Cada tool tiene:
- definition: el schema que se manda en `tools=[...]`
- handler: función que recibe `(tool_input: dict, ctx: ToolContext) -> dict|str`

ctx incluye group_id (puede ser None en DM owner) y un flag is_owner.

Tools que mutan estado (update_my_soul, create_kb, subscribe_kb, etc.) son
owner-only: si el solicitante no es owner, devuelven {ok:false, reason:owner_only}.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Optional

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


# ── Registry ────────────────────────────────────────────────────────
TOOL_DEFINITIONS = [
    LOOKUP_KB_FACT,
    LIST_KB_FACTS,
    REMEMBER_FACT,
    UPDATE_MY_SOUL,
    CREATE_KB,
    SUBSCRIBE_KB,
    RENAME_GROUP,
]

_HANDLERS: dict[str, Callable[[dict, ToolContext], Any]] = {
    "lookup_kb_fact": _handle_lookup_kb_fact,
    "list_kb_facts": _handle_list_kb_facts,
    "remember_fact": _handle_remember_fact,
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
