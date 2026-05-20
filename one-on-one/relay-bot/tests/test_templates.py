"""Render unit tests — no network."""
from __future__ import annotations

from iris_relay.templates import (
    build_inline_keyboard,
    render_approved,
    render_closed,
    render_reply_prompt,
    render_reply_sent,
    render_thread_messages,
    render_ticket_message,
    render_urgent_banner,
)


def _base_payload(**overrides):
    p = {
        "ticket_id": 42,
        "thread_id": 7,
        "kind": "consulta_cita",
        "summary": "Quiero agendar una consulta para mi hijo",
        "draft": None,
        "contact_phone": "+5215512345678",
        "contact_name": "María López",
        "urgent": False,
    }
    p.update(overrides)
    return p


def test_render_ticket_includes_icon_and_summary():
    msg = render_ticket_message(_base_payload())
    assert "📅" in msg
    assert "Consulta / cita" in msg
    assert "ticket #42" in msg
    assert "María López" in msg
    assert "+5215512345678" in msg
    assert "agendar una consulta" in msg


def test_render_ticket_escapes_html():
    msg = render_ticket_message(_base_payload(summary="<script>alert(1)</script>"))
    assert "<script>" not in msg
    assert "&lt;script&gt;" in msg


def test_render_ticket_includes_draft_when_present():
    msg = render_ticket_message(_base_payload(draft="Hola, te puedo agendar el martes 10am."))
    assert "Plantilla propuesta" in msg
    assert "martes 10am" in msg


def test_urgent_banner_and_header():
    payload = _base_payload(urgent=True, kind="urgencia_clinica")
    msg = render_ticket_message(payload)
    assert "🚨" in msg
    assert "URGENTE" in msg
    banner = render_urgent_banner(payload)
    assert "URGENTE" in banner
    assert "María López" in banner


def test_keyboard_without_draft_has_no_approve():
    kb = build_inline_keyboard(_base_payload())
    flat = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert "✅ Aprobar plantilla" not in flat
    assert "✍️ Responder" in flat
    assert "🚫 Rechazar/Cerrar" in flat
    assert "📋 Ver thread" in flat


def test_keyboard_with_draft_has_approve():
    kb = build_inline_keyboard(_base_payload(draft="hola"))
    flat = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert "✅ Aprobar plantilla" in flat


def test_keyboard_urgent_disables_reject():
    kb = build_inline_keyboard(_base_payload(urgent=True))
    flat = [b["text"] for row in kb["inline_keyboard"] for b in row]
    assert "🚫 Rechazar/Cerrar" not in flat
    # Thread button still present.
    assert "📋 Ver thread" in flat


def test_keyboard_callback_data_has_ticket_id():
    kb = build_inline_keyboard(_base_payload(draft="x"))
    flat = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert "approve:42" in flat
    assert "reply:42" in flat
    assert "close:42" in flat
    assert "thread:42:7" in flat


def test_render_misc():
    assert "Cerrado" in render_closed(42)
    assert "reply" in render_reply_prompt(42)
    assert "Respuesta enviada" in render_reply_sent(42, "ok")
    assert "Plantilla aprobada" in render_approved(42, "ok")


def test_render_thread_messages():
    msgs = [
        {"direction": "in", "ts": "2026-05-14T09:00", "body": "hola"},
        {"direction": "out", "ts": "2026-05-14T09:01", "body": "qué tal"},
    ]
    rendered = render_thread_messages(7, msgs)
    assert "Thread #7" in rendered
    assert "hola" in rendered
    assert "qué tal" in rendered


def test_render_thread_messages_empty():
    rendered = render_thread_messages(7, [])
    assert "Thread #7" in rendered
    assert "sin mensajes" in rendered
