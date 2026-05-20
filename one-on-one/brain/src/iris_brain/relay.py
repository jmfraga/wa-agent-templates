"""Clientes HTTP para relays.

Sprint 2 frozen decision:
- send_to_owner  → relay-bot Telegram (OWNER_RELAY_WEBHOOK, default :8098).
- send_to_contact → wa-listener (CONTACT_RELAY_WEBHOOK, default :8099).

El wa-listener exige `phone`, no `thread_id`; lo resolvemos consultando la DB.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select

from .config import settings
from .db import get_session
from .models import Contact, Thread

log = logging.getLogger("iris_brain.relay")


def _resolve_phone(thread_id: int) -> str | None:
    """Devuelve el phone del contacto dueño del thread, o None si no existe."""
    with get_session() as s:
        row = s.execute(
            select(Contact.phone)
            .join(Thread, Thread.contact_id == Contact.id)
            .where(Thread.id == thread_id)
        ).first()
        if row is None:
            return None
        return row[0]


class Relay:
    def __init__(
        self,
        owner_webhook_url: str | None = None,
        contact_webhook_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.owner_webhook_url = (
            owner_webhook_url if owner_webhook_url is not None else settings.OWNER_RELAY_WEBHOOK
        )
        self.contact_webhook_url = (
            contact_webhook_url
            if contact_webhook_url is not None
            else settings.CONTACT_RELAY_WEBHOOK
        )
        self._client = client or httpx.Client(timeout=10.0)

    def _post(self, url: str | None, payload: dict[str, Any], label: str) -> dict[str, Any]:
        if not url:
            log.warning("%s webhook no configurado, relay no-op: %s", label, payload.get("type"))
            return {"ok": False, "noop": True, "reason": "no_webhook"}
        try:
            r = self._client.post(url, json=payload)
            r.raise_for_status()
            return {"ok": True, "status": r.status_code}
        except httpx.HTTPError as e:
            log.exception("%s relay POST falló", label)
            return {"ok": False, "error": str(e)}

    def send_to_owner(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """Manda ticket al relay-bot Telegram para aprobación/respuesta de OWNER."""
        payload = {
            "type": "ticket_to_owner",
            "ticket_id": ticket.get("id") or ticket.get("ticket_id"),
            "thread_id": ticket.get("thread_id"),
            "kind": ticket.get("kind"),
            "summary": ticket.get("summary"),
            "draft_for_owner": ticket.get("draft_for_owner"),
            "urgent": ticket.get("urgent", False),
            "contact_phone": ticket.get("contact_phone"),
            "contact_name": ticket.get("contact_name"),
        }
        return self._post(self.owner_webhook_url, payload, label="owner")

    def send_to_contact(self, thread_id: int, body: str) -> dict[str, Any]:
        """Manda respuesta de OWNER al contacto vía wa-listener.

        El wa-listener necesita `phone` (no `thread_id`); lo resolvemos en DB.
        """
        phone = _resolve_phone(thread_id)
        if phone is None:
            log.warning("send_to_contact: thread_id=%s sin contacto en DB", thread_id)
            return {"ok": False, "error": "thread_sin_contacto", "thread_id": thread_id}
        payload = {
            "type": "reply_to_contact",
            "phone": phone,
            "body": body,
            "thread_id": thread_id,
        }
        return self._post(self.contact_webhook_url, payload, label="contact")


_default: Relay | None = None


def get_relay() -> Relay:
    global _default
    if _default is None:
        _default = Relay()
    return _default


def reset_relay() -> None:
    """Test hook: fuerza recrear el singleton la próxima vez."""
    global _default
    _default = None
