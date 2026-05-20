"""FastAPI server for iris-relay (port 8098).

Endpoints:
  POST /send-to-owner       — brain pushes a ticket here; we forward to Telegram.
  POST /send-to-contact   — explicitly 400, that path is wa-listener's job.
  GET  /health            — liveness + counts.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .config import Settings, get_settings
from .state import StateStore
from .telegram import TelegramRelay

log = logging.getLogger("iris_relay.server")


class SendToOwnerPayload(BaseModel):
    ticket_id: int
    thread_id: Optional[int] = None
    kind: str = "otro"
    summary: str
    draft: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_name: Optional[str] = None
    urgent: bool = False


def create_app(
    settings: Optional[Settings] = None,
    relay: Optional[TelegramRelay] = None,
) -> FastAPI:
    settings = settings or get_settings()
    state = StateStore(settings.state_db_url)
    relay = relay or TelegramRelay(settings=settings, state=state)

    app = FastAPI(title="iris-relay", version="0.1.0")
    app.state.settings = settings
    app.state.state = state
    app.state.relay = relay

    @app.on_event("startup")
    def _startup() -> None:  # pragma: no cover - touches threads
        logging.basicConfig(level=settings.log_level.upper())
        relay.start()
        log.info("iris-relay listening on :%s, brain=%s", settings.relay_bot_port, settings.brain_url)

    @app.on_event("shutdown")
    def _shutdown() -> None:  # pragma: no cover
        relay.stop()

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "telegram_connected": bool(settings.telegram_bot_token and settings.telegram_chat_id),
            "brain_url": settings.brain_url,
            "tickets_pending": state.count_pending(),
        }

    @app.post("/send-to-owner")
    def send_to_owner(payload: SendToOwnerPayload) -> dict[str, Any]:
        try:
            result = relay.send_ticket(payload.model_dump())
        except Exception as e:  # noqa: BLE001
            log.exception("send_ticket failed")
            raise HTTPException(status_code=502, detail=f"telegram_send_failed: {e}")
        return {"ok": True, **result}

    @app.post("/send-to-contact")
    def send_to_contact() -> dict[str, Any]:
        raise HTTPException(
            status_code=400,
            detail=(
                "send-to-contact is handled by wa-listener directly. "
                "This relay only ships tickets to OWNER on Telegram."
            ),
        )

    return app


# Module-level app for `uvicorn iris_relay.server:app` usage.
app = create_app()
