"""Tests para la regla colega_logistica (Phase 1c.fix).

Cuando un contacto kind=colega reporta logística (visitante llegó, espera,
tiene cita, reagenda), Iris NO devuelve la pelota — abre ticket urgente,
notifica al owner vía report_to_owner y manda acuse cálido al colega.
"""
from __future__ import annotations

import pytest

from iris_brain.chat import _is_colega_logistica


# ---------------------------------------------------------------------------
# Detector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "llegó Luis Miguel de Lab GSK, tiene cita con usted",
        "esta esperando el paciente",
        "Hola dr, tiene visita aquí",
        "Reagenda la cita de mañana",
        "Le aviso que el visitador llego",
        "Está en recepción esperando",
        "Hola dr. solo para avisarle que llegó Luis Miguel",
        "Tiene cita con el doctor a las 3",
        "Lo están esperando en consulta",
    ],
)
def test_logistica_matches(text: str) -> None:
    assert _is_colega_logistica(text) is True, f"esperaba match para: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "Hola dr, ¿cómo está?",
        "Feliz cumpleaños",
        "Gracias por todo",
        "¿Puede prescribirme algo?",
        "",
    ],
)
def test_no_logistica_matches(text: str) -> None:
    assert _is_colega_logistica(text) is False, f"NO debió matchear: {text!r}"


# ---------------------------------------------------------------------------
# Integración: handle_message con kind=colega
# ---------------------------------------------------------------------------


@pytest.fixture
def colega_phone() -> str:
    return "+5215512349999"


@pytest.fixture
def setup_colega(colega_phone):
    """Crea contacto kind=colega antes del test."""
    from iris_brain import sessions
    from iris_brain.db import get_session
    from iris_brain.models import Contact, ContactKind

    cid = sessions.upsert_contact(colega_phone)
    with get_session() as s:
        c = s.get(Contact, cid)
        c.kind = ContactKind.colega
        c.name = "Maricarmen Test"
    return colega_phone, cid


def test_colega_logistica_abre_ticket_y_reporta_owner(
    setup_colega, fake_anthropic, fake_relay, monkeypatch
):
    """Cuando colega reporta logística → ticket + report_to_owner + acuse cálido.

    Crítico: NO debe llamar send_to_contact (eso lo hace wa-listener al recibir
    el reply en el response del endpoint). El reply solo se devuelve y se
    persiste; el path NO invoca outbound a contacto en este branch.
    """
    from iris_brain import agentic as agentic_mod
    from iris_brain import chat

    # Capturar llamadas a report_to_owner sin hacer HTTP real.
    calls: list[dict] = []

    def _fake_report(text, task_id=None, contact_phone=None):
        calls.append(
            {"text": text, "task_id": task_id, "contact_phone": contact_phone}
        )
        return {"ok": True, "via": "fake"}

    monkeypatch.setattr(agentic_mod, "report_to_owner", _fake_report)

    phone, _cid = setup_colega
    res = chat.handle_message(phone, "Hola dr, llegó Luis Miguel, tiene cita con usted")

    assert res["ok"] is True
    assert res["intent"] == "colega_logistica"
    assert res["intent_confidence"] == 1.0
    assert res["model"] == "colega_logistica_ack"
    assert res["ticket_id"] is not None
    # Reply es acuse cálido SIN pregunta.
    assert "Gracias" in res["reply"]
    assert "?" not in res["reply"]
    assert "le aviso al doctor" in res["reply"].lower()
    # Owner fue notificado.
    assert len(calls) == 1
    assert "colega" in calls[0]["text"].lower()
    # phone se normaliza (sin '+') al persistir.
    assert calls[0]["contact_phone"] and calls[0]["contact_phone"].lstrip("+") == phone.lstrip("+")
    # NO se llamó send_to_contact directamente desde el brain.
    assert fake_relay.contact_calls == []


def test_colega_no_logistica_pasa_al_pipeline_normal(
    setup_colega, fake_anthropic, fake_relay
):
    """Si el colega solo saluda, NO dispara el branch — usa pipeline normal."""
    from iris_brain import chat

    phone, _ = setup_colega
    res = chat.handle_message(phone, "Hola dr, ¿cómo está?")
    assert res["intent"] != "colega_logistica"


def test_no_colega_logistica_keywords_no_dispara_branch(
    fake_anthropic, fake_relay
):
    """Contacto NO-colega aunque diga 'llegó' no entra al branch."""
    from iris_brain import chat

    # phone fresh → kind=otro por default
    res = chat.handle_message("+5215512340000", "llegó el paquete")
    assert res["intent"] != "colega_logistica"
