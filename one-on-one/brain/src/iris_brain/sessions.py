"""Sesiones día-contacto persistidas en Postgres.

session_id = "{YYYY-MM-DD}-{contact_phone_sanitized}". No es persistido como
columna; es solo una etiqueta lógica usada por el orquestador. La fuente de
verdad son las tablas messages/threads.
"""
from __future__ import annotations

import re
from datetime import date

from sqlalchemy import select

from .db import get_session
from .models import Contact, Message, MessageDirection, Thread, ThreadStatus

_PHONE_SANITIZE = re.compile(r"[^0-9]+")


def sanitize_phone(phone: str) -> str:
    return _PHONE_SANITIZE.sub("", phone or "")


def session_id_for(phone: str, *, on: date | None = None) -> str:
    day = (on or date.today()).strftime("%Y-%m-%d")
    return f"{day}-{sanitize_phone(phone)}"


def upsert_contact(phone: str, *, name: str | None = None) -> int:
    """Garantiza que el contacto existe. Devuelve contact_id."""
    p = sanitize_phone(phone)
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            c = Contact(phone=p, name=name)
            s.add(c)
            s.flush()
        elif name and not c.name:
            c.name = name
        s.flush()
        return c.id


def open_or_get_thread(contact_id: int) -> int:
    """Devuelve thread abierto del contacto o crea uno nuevo."""
    with get_session() as s:
        t = s.scalar(
            select(Thread)
            .where(Thread.contact_id == contact_id, Thread.status == ThreadStatus.open)
            .order_by(Thread.opened_at.desc())
        )
        if t is None:
            t = Thread(contact_id=contact_id)
            s.add(t)
            s.flush()
        return t.id


def append_message(
    thread_id: int,
    direction: MessageDirection,
    body: str,
    *,
    media_url: str | None = None,
    model_used: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
) -> int:
    with get_session() as s:
        m = Message(
            thread_id=thread_id,
            direction=direction,
            body=body,
            media_url=media_url,
            model_used=model_used,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
        )
        s.add(m)
        s.flush()
        return m.id


def history_for_model(thread_id: int, max_turns: int = 30) -> list[dict]:
    """Devuelve historial como [{role, content}] compatible con anthropic.messages."""
    with get_session() as s:
        msgs = list(
            s.scalars(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(Message.ts.asc())
            )
        )
        # Extract dentro de la sesión para evitar DetachedInstanceError.
        rows = [(m.direction, m.body) for m in msgs]
    if max_turns > 0:
        rows = rows[-max_turns:]
    out: list[dict] = []
    for direction, body in rows:
        role = "user" if direction == MessageDirection.in_.value else "assistant"
        out.append({"role": role, "content": body})
    while out and out[0]["role"] != "user":
        out = out[1:]
    return out
