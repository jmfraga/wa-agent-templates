"""Historia conversacional por grupo (window N últimas)."""
from typing import Optional

from sqlalchemy import select

from .config import settings
from .db import get_session
from .models import Message


def history_for_group(group_id: int, limit: Optional[int] = None) -> list[dict]:
    """Devuelve mensajes recientes del grupo formateados para Anthropic.
    Más antiguo primero. Inserta el nombre del autor en mensajes 'in' para
    que el modelo distinga quién dijo qué."""
    limit = limit or settings.history_window
    with get_session() as s:
        rows = s.execute(
            select(Message)
            .where(Message.group_id == group_id)
            .order_by(Message.ts.desc())
            .limit(limit)
        ).scalars().all()

    return _format_rows(list(reversed(rows)))


def history_for_contact(contact_jid: str, limit: Optional[int] = None) -> list[dict]:
    """Mensajes 1:1 con un contacto (group_id is NULL). Mismo formato que
    history_for_group. Útil para DMs con owner u otros contactos directos."""
    limit = limit or settings.history_window
    with get_session() as s:
        rows = s.execute(
            select(Message)
            .where(Message.group_id.is_(None), Message.contact_jid == contact_jid)
            .order_by(Message.ts.desc())
            .limit(limit)
        ).scalars().all()
    return _format_rows(list(reversed(rows)))


def _format_rows(rows) -> list[dict]:
    msgs: list[dict] = []
    for m in rows:
        if m.direction == "in":
            who = m.contact_name or m.contact_jid or "alguien"
            body = m.body or (f"[{m.media_hint}]" if m.media_hint else "")
            msgs.append({"role": "user", "content": f"{who}: {body}"})
        else:
            msgs.append({"role": "assistant", "content": m.body or ""})
    return msgs


def record_inbound(
    *,
    group_id: Optional[int],
    contact_jid: Optional[str],
    contact_name: Optional[str],
    text: str,
    media_hint: Optional[str],
    quoted_msg_id: Optional[str],
    mentions_phoenix: bool,
) -> int:
    with get_session() as s:
        m = Message(
            group_id=group_id,
            contact_jid=contact_jid,
            contact_name=contact_name,
            direction="in",
            body=text,
            media_hint=media_hint,
            quoted_msg_id=quoted_msg_id,
            mentions_phoenix=mentions_phoenix,
        )
        s.add(m)
        s.commit()
        return m.id


def record_outbound(
    *,
    group_id: Optional[int],
    text: str,
    model_used: str,
    tokens_in: int,
    tokens_out: int,
    contact_jid: Optional[str] = None,
) -> int:
    """Persiste la respuesta de Phoenix. En DMs, contact_jid es el destinatario
    (para que history_for_contact pueda recuperar la conversación completa)."""
    with get_session() as s:
        m = Message(
            group_id=group_id,
            contact_jid=contact_jid,
            direction="out",
            body=text,
            model_used=model_used,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
        )
        s.add(m)
        s.commit()
        return m.id
