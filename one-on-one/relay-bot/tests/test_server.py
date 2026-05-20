"""Server-level tests with a mocked TelegramRelay (no network)."""
from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from iris_relay.config import Settings
from iris_relay.server import create_app
from iris_relay.state import StateStore


@pytest.fixture()
def app_and_relay(tmp_path):
    db_path = tmp_path / "state.db"
    settings = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="12345",
        BRAIN_URL="http://brain.test",
        RELAY_BOT_PORT=8098,
        STATE_DB_PATH=str(db_path),
    )
    state = StateStore(settings.state_db_url)
    relay = MagicMock()
    relay.send_ticket.return_value = {"telegram_message_id": 999, "ticket_id": 42}
    app = create_app(settings=settings, relay=relay)
    # Prevent the lifecycle from starting the real polling loop in tests.
    relay.start = MagicMock()
    relay.stop = MagicMock()
    return app, relay, state


def test_health(app_and_relay):
    app, relay, state = app_and_relay
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["telegram_connected"] is True
    assert data["brain_url"] == "http://brain.test"
    assert data["tickets_pending"] == 0


def test_send_to_jmf_dispatches(app_and_relay):
    app, relay, state = app_and_relay
    payload = {
        "ticket_id": 42,
        "thread_id": 7,
        "kind": "consulta_cita",
        "summary": "agendar",
        "draft": None,
        "contact_phone": "+5215512345678",
        "contact_name": "María",
        "urgent": False,
    }
    with TestClient(app) as client:
        r = client.post("/send-to-jmf", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["telegram_message_id"] == 999
    relay.send_ticket.assert_called_once()
    called = relay.send_ticket.call_args[0][0]
    assert called["ticket_id"] == 42
    assert called["kind"] == "consulta_cita"


def test_send_to_contact_rejected(app_and_relay):
    app, _, _ = app_and_relay
    with TestClient(app) as client:
        r = client.post("/send-to-contact", json={})
    assert r.status_code == 400
    assert "wa-listener" in r.json()["detail"]


def test_send_to_jmf_validates_payload(app_and_relay):
    app, _, _ = app_and_relay
    with TestClient(app) as client:
        r = client.post("/send-to-jmf", json={"summary": "no ticket id"})
    assert r.status_code == 422
