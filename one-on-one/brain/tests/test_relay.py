"""Pruebas del Relay class — mock httpx.Client."""
from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest


@pytest.fixture
def mock_httpx_client():
    c = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    c.post = MagicMock(return_value=resp)
    return c


def test_send_to_owner_usa_owner_webhook(mock_httpx_client):
    from iris_brain.relay import Relay

    r = Relay(
        owner_webhook_url="http://owner.local/send-to-owner",
        contact_webhook_url="http://contact.local/send-to-contact",
        client=mock_httpx_client,
    )
    out = r.send_to_owner(
        {
            "id": 1,
            "thread_id": 2,
            "kind": "consulta_cita",
            "summary": "test",
            "draft_for_owner": None,
            "urgent": False,
            "contact_phone": "5215512345678",
            "contact_name": "Juan",
        }
    )
    assert out["ok"] is True
    mock_httpx_client.post.assert_called_once()
    args, kwargs = mock_httpx_client.post.call_args
    assert args[0] == "http://owner.local/send-to-owner"
    payload = kwargs["json"]
    assert payload["contact_phone"] == "5215512345678"
    assert payload["contact_name"] == "Juan"
    assert payload["type"] == "ticket_to_owner"


def test_send_to_contact_resuelve_phone_y_usa_contact_webhook(mock_httpx_client):
    """send_to_contact debe consultar la DB para resolver phone desde thread_id."""
    from iris_brain import sessions
    from iris_brain.relay import Relay

    contact_id = sessions.upsert_contact("+5215599998888", name="Test")
    thread_id = sessions.open_or_get_thread(contact_id)

    r = Relay(
        owner_webhook_url="http://owner.local/send-to-owner",
        contact_webhook_url="http://contact.local/send-to-contact",
        client=mock_httpx_client,
    )
    out = r.send_to_contact(thread_id, "Hola, te confirmo la cita.")
    assert out["ok"] is True
    args, kwargs = mock_httpx_client.post.call_args
    assert args[0] == "http://contact.local/send-to-contact"
    payload = kwargs["json"]
    assert payload["phone"] == "5215599998888"
    assert payload["body"] == "Hola, te confirmo la cita."
    assert payload["thread_id"] == thread_id


def test_send_to_contact_thread_inexistente(mock_httpx_client):
    from iris_brain.relay import Relay

    r = Relay(
        owner_webhook_url="http://owner.local",
        contact_webhook_url="http://contact.local",
        client=mock_httpx_client,
    )
    out = r.send_to_contact(99999, "x")
    assert out["ok"] is False
    assert out["error"] == "thread_sin_contacto"
    mock_httpx_client.post.assert_not_called()


def test_send_to_owner_sin_webhook_noop(mock_httpx_client, monkeypatch):
    from iris_brain import relay as relay_mod
    from iris_brain.relay import Relay

    monkeypatch.setattr(relay_mod.settings, "OWNER_RELAY_WEBHOOK", None)
    monkeypatch.setattr(relay_mod.settings, "CONTACT_RELAY_WEBHOOK", None)
    r = Relay(
        owner_webhook_url=None,
        contact_webhook_url=None,
        client=mock_httpx_client,
    )
    out = r.send_to_owner({"id": 1, "thread_id": 1, "kind": "x", "summary": "y"})
    assert out["ok"] is False
    assert out["noop"] is True
    mock_httpx_client.post.assert_not_called()


def test_send_to_contact_sin_webhook_noop(mock_httpx_client, monkeypatch):
    from iris_brain import relay as relay_mod
    from iris_brain import sessions
    from iris_brain.relay import Relay

    monkeypatch.setattr(relay_mod.settings, "CONTACT_RELAY_WEBHOOK", None)
    cid = sessions.upsert_contact("+5215577776666")
    tid = sessions.open_or_get_thread(cid)
    r = Relay(
        owner_webhook_url="http://owner.local",
        contact_webhook_url=None,
        client=mock_httpx_client,
    )
    out = r.send_to_contact(tid, "hi")
    assert out["ok"] is False
    assert out.get("noop") is True
    mock_httpx_client.post.assert_not_called()


def test_send_to_owner_http_error_se_captura(mock_httpx_client):
    from iris_brain.relay import Relay

    mock_httpx_client.post.return_value.raise_for_status = MagicMock(
        side_effect=httpx.HTTPError("boom")
    )
    r = Relay(
        owner_webhook_url="http://owner.local",
        contact_webhook_url="http://contact.local",
        client=mock_httpx_client,
    )
    out = r.send_to_owner({"id": 1, "thread_id": 1, "kind": "x", "summary": "y"})
    assert out["ok"] is False
    assert "boom" in out["error"]
