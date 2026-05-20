"""Mock del wa-listener — sólo loggea las llamadas a `/send-to-contact`
y devuelve 200. Útil cuando corres brain + tester sin Baileys real.

Run:
    uvicorn iris_tester.mock_wa_listener:app --port 8099
    # o
    python -m iris_tester.mock_wa_listener
"""

from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="iris-mock-wa-listener", version="0.1.0")

# Memoria en proceso: últimos N mensajes entregados. Útil para los tests.
SENT_LOG: list[dict] = []


class SendToContactRequest(BaseModel):
    phone: str
    body: str
    thread_id: Optional[str] = None


class SendToContactResponse(BaseModel):
    ok: bool
    message_id: Optional[str] = None
    error: Optional[str] = None


@app.get("/health")
def health() -> dict:
    return {"ok": True, "connected": True, "port": int(os.environ.get("WA_LISTENER_PORT", 8099))}


@app.post("/send-to-contact", response_model=SendToContactResponse)
def send_to_contact(req: SendToContactRequest) -> SendToContactResponse:
    message_id = f"mock_{uuid.uuid4().hex[:12]}"
    entry = {
        "phone": req.phone,
        "body": req.body,
        "thread_id": req.thread_id,
        "message_id": message_id,
    }
    SENT_LOG.append(entry)
    print(f"[mock-wa] send-to-contact {entry}")
    return SendToContactResponse(ok=True, message_id=message_id)


@app.get("/_debug/sent")
def debug_sent() -> dict:
    """Endpoint de inspección para los tests."""
    return {"count": len(SENT_LOG), "items": SENT_LOG[-50:]}


@app.post("/_debug/reset")
def debug_reset() -> dict:
    SENT_LOG.clear()
    return {"ok": True}


def run() -> None:
    """Entry point para `iris-mock-wa`."""
    import uvicorn

    port = int(os.environ.get("WA_LISTENER_PORT", 8099))
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    run()
