"""Discussion launcher: comandos slash por DM del owner.

Comandos soportados (texto enviado por el owner en DM):
  /lanza <grupo> <tema...>      → genera draft + ID, status=draft
  /publica <id>                 → publica vía listener; status=posted
  /cancela <id>                 → status=cancelled
  /edita <id> <nuevo texto>     → reemplaza draft (sigue en status=draft)
  /drafts                       → lista los pendientes
  /lanza                        → muestra ayuda

`<grupo>` puede ser:
  - JID exacto (algo@g.us)
  - substring case-insensitive de Group.display_name; si matchea único, lo toma.
    Si hay ambiguedad o 0 matches, devuelve mensaje pidiendo precisión.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx
from sqlalchemy import select

from . import anthropic_client
from .config import top
from .db import get_session
from .models import DiscussionStarter, Group, GroupSoul

SLASH_PREFIX_RE = re.compile(r"^\s*/(lanza|publica|cancela|edita|drafts)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)


@dataclass
class CommandResult:
    reply: str  # texto que Phoenix devuelve al owner por DM


# ── Helpers ──────────────────────────────────────────────────────────
def _find_group(token: str) -> tuple[Optional[Group], Optional[str]]:
    """Devuelve (group, error_message)."""
    token = token.strip()
    if not token:
        return None, "falta el nombre o JID del grupo."
    with get_session() as s:
        if token.endswith("@g.us"):
            g = s.execute(select(Group).where(Group.wa_jid == token)).scalar_one_or_none()
            if g is None:
                return None, f"no encontré ningún grupo con JID {token}."
            s.expunge(g)
            return g, None
        # Fuzzy: substring en display_name.
        rows = s.execute(
            select(Group).where(Group.is_active == True)  # noqa: E712
        ).scalars().all()
        matches = [g for g in rows if token.lower() in (g.display_name or "").lower()]
        if not matches:
            return None, f"no encontré grupo que contenga '{token}'."
        if len(matches) > 1:
            names = ", ".join(g.display_name for g in matches[:5])
            return None, f"'{token}' es ambiguo: {names}. Usa el JID exacto."
        g = matches[0]
        s.expunge(g)
        return g, None


def _group_soul_text(group_id: int) -> Optional[str]:
    with get_session() as s:
        soul = s.execute(
            select(GroupSoul)
            .where(GroupSoul.group_id == group_id, GroupSoul.is_active == True)  # noqa: E712
            .order_by(GroupSoul.version.desc())
        ).scalar_one_or_none()
        return soul.soul_md if soul else None


def _generate_draft(group: Group, topic: str) -> str:
    """Usa Sonnet 4.6 para redactar el primer mensaje al grupo."""
    soul = _group_soul_text(group.id) or ""
    system = (
        "Eres Phoenix. <OWNER> (el owner) te está pidiendo que lances una discusión en "
        f"el grupo '{group.display_name}'. Redacta UN SOLO mensaje, primera persona, "
        "tono natural del grupo (no muy largo, 2-4 párrafos cortos máximo), que "
        "invite a participar SIN cerrar la conversación. Termina con UNA pregunta "
        "abierta. No incluyas '@' ni menciones. No firmes. Español de México.\n\n"
        f"SOUL del grupo (para que mantengas el rol y el tono):\n---\n{soul}"
    )
    client = anthropic_client.get_client()
    resp = client.messages.create(
        model=anthropic_client.model_safety(),  # Sonnet 4.6
        max_tokens=600,
        system=system,
        messages=[
            {"role": "user", "content": f"Tema a lanzar:\n{topic}"},
        ],
    )
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    return text or topic


# ── Comandos ─────────────────────────────────────────────────────────
def _cmd_lanza(rest: str) -> CommandResult:
    rest = rest.strip()
    if not rest:
        return CommandResult(
            reply=(
                "Uso: /lanza <grupo> <tema>\n"
                "  <grupo> = JID (xxx@g.us) o parte del nombre (ej. 'oficina')\n"
                "  <tema>  = en lenguaje natural lo que quieres que lance\n\n"
                "Otros comandos: /drafts, /publica <id>, /cancela <id>, /edita <id> <texto>"
            )
        )
    parts = rest.split(None, 1)
    if len(parts) < 2:
        return CommandResult(reply="falta el tema. Uso: /lanza <grupo> <tema>")
    group_token, topic = parts[0], parts[1]
    group, err = _find_group(group_token)
    if err:
        return CommandResult(reply=err)
    draft_text = _generate_draft(group, topic)
    with get_session() as s:
        ds = DiscussionStarter(
            group_id=group.id,
            topic=topic,
            prompt=draft_text,
            status="draft",
            triggered_by="jmf",
        )
        s.add(ds)
        s.commit()
        s.refresh(ds)
        ds_id = ds.id
    return CommandResult(
        reply=(
            f"Borrador #{ds_id} para *{group.display_name}*:\n\n"
            f"{draft_text}\n\n"
            f"— Responde: /publica {ds_id}  ·  /edita {ds_id} <texto>  ·  /cancela {ds_id}"
        )
    )


def _post_to_listener(group_jid: str, text: str) -> tuple[bool, Optional[str]]:
    url = top.phoenix_listener_url.rstrip("/") + "/post-to-group"
    try:
        r = httpx.post(url, json={"group_jid": group_jid, "text": text}, timeout=15.0)
        if r.status_code >= 400:
            return False, f"listener devolvió {r.status_code}: {r.text[:200]}"
        return True, None
    except Exception as e:  # noqa: BLE001
        return False, f"no pude contactar al listener: {e}"


def _cmd_publica(rest: str) -> CommandResult:
    rest = rest.strip()
    if not rest.isdigit():
        return CommandResult(reply="uso: /publica <id>")
    ds_id = int(rest)
    with get_session() as s:
        ds = s.get(DiscussionStarter, ds_id)
        if not ds:
            return CommandResult(reply=f"borrador #{ds_id} no existe.")
        if ds.status != "draft":
            return CommandResult(reply=f"borrador #{ds_id} ya está en status={ds.status}.")
        group = s.get(Group, ds.group_id)
        if not group:
            return CommandResult(reply="el grupo asociado ya no existe.")
        group_jid = group.wa_jid
        group_name = group.display_name
        text = ds.prompt or ""

    ok, err = _post_to_listener(group_jid, text)
    if not ok:
        return CommandResult(reply=f"❌ {err}\nEl borrador sigue en draft, puedes reintentar /publica {ds_id}.")

    with get_session() as s:
        ds = s.get(DiscussionStarter, ds_id)
        if ds:
            ds.status = "posted"
            ds.posted_at = datetime.utcnow()
            s.commit()
    return CommandResult(reply=f"✅ publicado en *{group_name}* (#{ds_id}).")


def _cmd_cancela(rest: str) -> CommandResult:
    rest = rest.strip()
    if not rest.isdigit():
        return CommandResult(reply="uso: /cancela <id>")
    ds_id = int(rest)
    with get_session() as s:
        ds = s.get(DiscussionStarter, ds_id)
        if not ds:
            return CommandResult(reply=f"borrador #{ds_id} no existe.")
        if ds.status != "draft":
            return CommandResult(reply=f"borrador #{ds_id} ya está en status={ds.status}.")
        ds.status = "cancelled"
        s.commit()
    return CommandResult(reply=f"borrador #{ds_id} cancelado.")


def _cmd_edita(rest: str) -> CommandResult:
    parts = rest.strip().split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        return CommandResult(reply="uso: /edita <id> <texto nuevo>")
    ds_id = int(parts[0])
    new_text = parts[1].strip()
    with get_session() as s:
        ds = s.get(DiscussionStarter, ds_id)
        if not ds:
            return CommandResult(reply=f"borrador #{ds_id} no existe.")
        if ds.status != "draft":
            return CommandResult(reply=f"borrador #{ds_id} ya está en status={ds.status}.")
        ds.prompt = new_text
        s.commit()
        group = s.get(Group, ds.group_id)
        group_name = group.display_name if group else "?"
    return CommandResult(
        reply=(
            f"Borrador #{ds_id} actualizado para *{group_name}*:\n\n{new_text}\n\n"
            f"— Responde: /publica {ds_id}  ·  /edita {ds_id} <texto>  ·  /cancela {ds_id}"
        )
    )


def _cmd_drafts(_rest: str) -> CommandResult:
    with get_session() as s:
        rows = s.execute(
            select(DiscussionStarter, Group.display_name)
            .join(Group, Group.id == DiscussionStarter.group_id)
            .where(DiscussionStarter.status == "draft")
            .order_by(DiscussionStarter.created_at.desc())
        ).all()
    if not rows:
        return CommandResult(reply="(no hay borradores pendientes)")
    lines = ["Borradores pendientes:"]
    for ds, gname in rows[:20]:
        preview = (ds.prompt or "")[:80].replace("\n", " ")
        lines.append(f"  #{ds.id}  {gname}  — {preview}{'…' if len(ds.prompt or '') > 80 else ''}")
    return CommandResult(reply="\n".join(lines))


_HANDLERS = {
    "lanza": _cmd_lanza,
    "publica": _cmd_publica,
    "cancela": _cmd_cancela,
    "edita": _cmd_edita,
    "drafts": _cmd_drafts,
}


# ── Entry point ──────────────────────────────────────────────────────
def maybe_handle_owner_command(text: str) -> Optional[CommandResult]:
    """Si el texto es un slash command soportado, lo procesa y devuelve la
    respuesta. Si no, devuelve None (el chat sigue al loop normal)."""
    m = SLASH_PREFIX_RE.match(text)
    if not m:
        return None
    cmd = m.group(1).lower()
    rest = m.group(2) or ""
    handler = _HANDLERS.get(cmd)
    if not handler:
        return None
    try:
        return handler(rest)
    except Exception as e:  # noqa: BLE001
        return CommandResult(reply=f"❌ error procesando /{cmd}: {e}")


def start_discussion_api(group_jid: str, topic: str, *, auto_publish: bool = False) -> dict:
    """Para endpoint HTTP (UI futura / curl). Genera draft, opcionalmente publica."""
    group, err = _find_group(group_jid)
    if err:
        return {"ok": False, "error": err}
    draft = _generate_draft(group, topic)
    with get_session() as s:
        ds = DiscussionStarter(
            group_id=group.id,
            topic=topic,
            prompt=draft,
            status="draft",
            triggered_by="api",
        )
        s.add(ds)
        s.commit()
        s.refresh(ds)
        ds_id = ds.id
    if not auto_publish:
        return {"ok": True, "id": ds_id, "draft": draft, "status": "draft"}
    ok, perr = _post_to_listener(group.wa_jid, draft)
    if not ok:
        return {"ok": False, "id": ds_id, "draft": draft, "status": "draft", "error": perr}
    with get_session() as s:
        ds = s.get(DiscussionStarter, ds_id)
        if ds:
            ds.status = "posted"
            ds.posted_at = datetime.utcnow()
            s.commit()
    return {"ok": True, "id": ds_id, "draft": draft, "status": "posted"}
