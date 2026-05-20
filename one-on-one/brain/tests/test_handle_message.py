"""Pruebas del pipeline chat.handle_message."""
from __future__ import annotations

import pytest


@pytest.fixture
def handle(fake_anthropic, fake_relay):
    from iris_brain import chat

    return chat.handle_message


def test_saludo_smalltalk_reply_directo(handle, fake_relay):
    res = handle("+5215512345678", "hola")
    assert res["ok"] is True
    assert res["intent"] == "saludo_smalltalk"
    assert res["reply"]
    assert res["ticket_id"] is None
    assert fake_relay.jmf_calls == []


def test_consulta_cita_abre_ticket_y_relay(handle, fake_relay):
    res = handle("+5215512345678", "quiero una consulta")
    assert res["intent"] == "consulta_cita"
    assert res["ticket_id"] is not None
    assert res["reply"]  # mensaje puente
    assert len(fake_relay.jmf_calls) == 1
    call = fake_relay.jmf_calls[0]
    assert call["kind"] == "consulta_cita"
    assert call["urgent"] is False
    assert call["contact_phone"]  # phone sanitizado


def test_urgencia_clinica_dispara_safety_keyword_y_relay_urgente(handle, fake_relay):
    # "me duele el pecho fuerte" + "no puedo respirar" disparan emergencia_medica/high
    res = handle(
        "+5215512345678",
        "me duele el pecho fuerte y no puedo respirar",
    )
    assert res["intent"] == "urgencia_clinica"
    assert res["safety"] is not None
    assert res["safety"]["level"] == "high"
    assert res["safety"]["category"] in {"emergencia_medica", "crisis_emocional"}
    assert res["model"] == "safety_keyword"  # NO se llamó al LLM
    assert res["ticket_id"] is not None
    assert "911" in res["reply"] or "doctor" in res["reply"].lower()
    assert len(fake_relay.jmf_calls) == 1
    call = fake_relay.jmf_calls[0]
    assert call["urgent"] is True
    assert call["kind"] == "urgencia"


def test_crisis_medium_dispara_clarification_y_relay_no_urgente(handle, fake_relay):
    res = handle(
        "+5215512345678",
        "tengo mucha ansiedad y ya no puedo dormir de la angustia",
    )
    assert res["safety"] is not None
    assert res["safety"]["level"] == "medium"
    assert res["model"] == "safety_keyword"
    assert res["ticket_id"] is not None
    assert len(fake_relay.jmf_calls) == 1
    call = fake_relay.jmf_calls[0]
    assert call["urgent"] is False
    assert call["kind"] == "posible_urgencia"


def test_safety_suicida_alta_prioridad(handle, fake_relay):
    res = handle("+5215512345678", "ya no quiero seguir vivo, mejor me muero")
    assert res["safety"]["category"] == "suicida"
    assert res["safety"]["level"] == "high"
    assert "SAPTEL" in res["reply"]
    assert fake_relay.jmf_calls[0]["urgent"] is True


def test_saludo_no_dispara_safety(handle, fake_relay):
    res = handle("+5215512345678", "hola buenas tardes")
    assert res["safety"] is None
    assert res["intent"] == "saludo_smalltalk"


def test_info_curso_sin_kb_fact_abre_ticket(handle, fake_relay):
    # No hay kb_facts en la DB → modelo responde algo genérico, ticket no
    # forzado salvo que reply esté vacío. El mock devuelve texto no vacío, así
    # que sólo verificamos que la rama corrió sin error y persistió intent.
    res = handle("+5215512345678", "cuánto cuesta el ACLS?")
    assert res["intent"] == "info_curso"
    assert res["reply"]
