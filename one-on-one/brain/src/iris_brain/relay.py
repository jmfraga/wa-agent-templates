"""Clientes HTTP para relays.

Sprint 2 frozen decision:
- send_to_jmf  → relay-bot Telegram (JMF_RELAY_WEBHOOK, default :8098).
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
        jmf_webhook_url: str | None = None,
        contact_webhook_url: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.jmf_webhook_url = (
            jmf_webhook_url if jmf_webhook_url is not None else settings.JMF_RELAY_WEBHOOK
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

    def send_to_jmf(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """Manda ticket al relay-bot Telegram para aprobación/respuesta de Owner."""
        payload = {
            "type": "ticket_to_jmf",
            "ticket_id": ticket.get("id") or ticket.get("ticket_id"),
            "thread_id": ticket.get("thread_id"),
            "kind": ticket.get("kind"),
            "summary": ticket.get("summary"),
            "draft_for_jmf": ticket.get("draft_for_jmf"),
            "urgent": ticket.get("urgent", False),
            "contact_phone": ticket.get("contact_phone"),
            "contact_name": ticket.get("contact_name"),
        }
        return self._post(self.jmf_webhook_url, payload, label="jmf")

    def send_media_to_contact(
        self,
        thread_id: int,
        phone: str,
        asset_id: int,
        caption: str | None = None,
        body_text: str | None = None,
    ) -> dict[str, Any]:
        """Envía imagen al contacto vía wa-listener.

        Si body_text viene, wa-listener manda primero el texto (con URL preview) y
        después la imagen con caption corto. Si no hay body_text, va solo la imagen
        con caption.
        """
        if not self.contact_webhook_url:
            return {"ok": False, "noop": True, "reason": "no_webhook"}
        media_url = f"{settings.MEDIA_INTERNAL_URL.rstrip('/')}/media/{asset_id}/raw"
        payload: dict[str, Any] = {
            "type": "outbound_media",
            "phone": phone,
            "thread_id": thread_id,
            "media": {
                "type": "image",
                "url": media_url,
                "caption": caption,
            },
        }
        if body_text:
            payload["body"] = body_text
        return self._post(self.contact_webhook_url, payload, label="contact_media")

    def send_to_contact(self, thread_id: int, body: str) -> dict[str, Any]:
        """Manda respuesta de Owner al contacto vía wa-listener.

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
