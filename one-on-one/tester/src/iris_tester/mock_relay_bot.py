"""Mock del relay-bot — recibe `/send-to-owner` y opcionalmente auto-responde
llamando al brain `/owner/reply` después de N segundos.

Útil para probar el ciclo completo paciente→OWNER→paciente sin Telegram real.

Run:
    iris-mock-relay
    # o
    BRAIN_URL=http://localhost:8096 \
    MOCK_RELAY_AUTO_REPLY="Confirmado lunes 4pm" \
    MOCK_RELAY_AUTO_DELAY=3 \
    python -m iris_tester.mock_relay_bot
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

BRAIN_URL = os.environ.get("BRAIN_URL", "http://localhost:8096")
AUTO_REPLY = os.environ.get("MOCK_RELAY_AUTO_REPLY")  # None → no auto-reply
AUTO_DELAY = float(os.environ.get("MOCK_RELAY_AUTO_DELAY", "2.0"))

app = FastAPI(title="iris-mock-relay-bot", version="0.1.0")

OWNER_INBOX: list[dict] = []


class SendToOwnerRequest(BaseModel):
    ticket_id: str
    contact_phone: str
    summary: str
    text: str


class SendToOwnerResponse(BaseModel):
    ok: bool
    delivered_to: str = "mock-owner"


async def _auto_reply_task(ticket_id: str, contact_phone: str, body: str) -> None:
    await asyncio.sleep(AUTO_DELAY)
    payload = {
        "ticket_id": ticket_id,
        "contact_phone": contact_phone,
        "text": body,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(f"{BRAIN_URL}/owner/reply", json=payload)
            r.raise_for_status()
            print(f"[mock-relay] auto-replied ticket={ticket_id} status={r.status_code}")
    except httpx.HTTPError as err:
        print(f"[mock-relay] auto-reply FAILED ticket={ticket_id}: {err}")


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "auto_reply_enabled": AUTO_REPLY is not None,
        "auto_delay_s": AUTO_DELAY,
        "brain_url": BRAIN_URL,
    }


@app.post("/send-to-owner", response_model=SendToOwnerResponse)
async def send_to_owner(req: SendToOwnerRequest) -> SendToOwnerResponse:
    entry = req.model_dump()
    OWNER_INBOX.append(entry)
    print(f"[mock-relay] ticket→OWNER {entry}")

    if AUTO_REPLY:
        # Disparamos la auto-respuesta sin bloquear la respuesta HTTP.
        asyncio.create_task(_auto_reply_task(req.ticket_id, req.contact_phone, AUTO_REPLY))

    return SendToOwnerResponse(ok=True)


@app.post("/_debug/manual-reply")
async def manual_reply(ticket_id: str, contact_phone: str, body: str) -> dict:
    """Para tests: dispara manualmente un /owner/reply al brain."""
    await _auto_reply_task(ticket_id, contact_phone, body)
    return {"ok": True}


@app.get("/_debug/inbox")
def debug_inbox() -> dict:
    return {"count": len(OWNER_INBOX), "items": OWNER_INBOX[-50:]}


@app.post("/_debug/reset")
def debug_reset() -> dict:
    OWNER_INBOX.clear()
    return {"ok": True}


def run() -> None:
    """Entry point para `iris-mock-relay`."""
    import uvicorn

    port = int(os.environ.get("RELAY_BOT_PORT", 8098))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    run()
