"""Fixtures globales para tests del brain.

- DB: SQLite in-memory por test (StaticPool para compartir entre conexiones).
- Anthropic client: mock con respuestas canned por intent / por modelo.
- FastAPI TestClient.
- Relay: monkeypatched para registrar llamadas sin hacer HTTP real.
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable
from unittest.mock import MagicMock

# Configurar env vars ANTES de importar iris_brain.
os.environ.setdefault("IRIS_BRAIN_DB_URL", "sqlite:///:memory:")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-dummy")
os.environ.setdefault("OWNER_RELAY_WEBHOOK", "http://test-owner-relay.local/send-to-owner")
os.environ.setdefault("CONTACT_RELAY_WEBHOOK", "http://test-contact-relay.local/send-to-contact")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool


# ---------------------------------------------------------------------------
# DB in-memory
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    """Override engine/SessionLocal del módulo iris_brain.db con SQLite in-mem.

    Se aplica autouse para que cualquier test importando iris_brain use SQLite.
    """
    from iris_brain import db as db_mod
    from iris_brain.models import Base

    test_engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    TestSessionLocal = sessionmaker(
        bind=test_engine, autoflush=False, autocommit=False, future=True
    )
    Base.metadata.create_all(test_engine)

    monkeypatch.setattr(db_mod, "engine", test_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", TestSessionLocal)
    yield test_engine
    Base.metadata.drop_all(test_engine)
    test_engine.dispose()


# ---------------------------------------------------------------------------
# Mock Anthropic client (intents + safety + chat)
# ---------------------------------------------------------------------------


def _mk_text_block(text: str) -> MagicMock:
    b = MagicMock()
    b.type = "text"
    b.text = text
    return b


def _mk_response(text: str, stop_reason: str = "end_turn") -> MagicMock:
    r = MagicMock()
    r.content = [_mk_text_block(text)]
    r.stop_reason = stop_reason
    usage = MagicMock()
    usage.input_tokens = 10
    usage.output_tokens = 5
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    r.usage = usage
    return r


class FakeAnthropicClient:
    """Mock que rutea por system prompt / último mensaje.

    Heurística:
    - Si system contiene "clasificador de intents" → devuelve JSON intent.
    - Si system contiene "detector de crisis" → devuelve JSON crisis.
    - En otro caso → respuesta canned del SOUL/chat.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = MagicMock()
        self.messages.create = self._create

    def _classify_text(self, text: str) -> dict[str, Any]:
        t = text.lower()
        if "dolor de pecho" in t or "no puedo respirar" in t or "urgenc" in t:
            return {"intent": "urgencia_clinica", "confidence": 0.95, "reasoning": "test"}
        if "consulta" in t or "agendar" in t or "cita" in t:
            return {"intent": "consulta_cita", "confidence": 0.9, "reasoning": "test"}
        if "acls" in t or "curso" in t or "cuesta" in t:
            return {"intent": "info_curso", "confidence": 0.85, "reasoning": "test"}
        if t.startswith("hola") or "buenos dias" in t or "gracias" in t:
            return {"intent": "saludo_smalltalk", "confidence": 0.95, "reasoning": "test"}
        return {"intent": "otro", "confidence": 0.5, "reasoning": "test"}

    def _last_user_text(self, messages: list[dict]) -> str:
        for m in reversed(messages or []):
            if m.get("role") == "user":
                c = m.get("content", "")
                if isinstance(c, str):
                    return c
                if isinstance(c, list):
                    for block in c:
                        if isinstance(block, dict) and block.get("type") == "text":
                            return block.get("text", "")
        return ""

    def _create(self, **kwargs: Any) -> MagicMock:
        self.calls.append(kwargs)
        system = kwargs.get("system", [])
        sys_text = ""
        if isinstance(system, list):
            for b in system:
                if isinstance(b, dict):
                    sys_text += b.get("text", "")
        elif isinstance(system, str):
            sys_text = system

        msgs = kwargs.get("messages", [])
        last = self._last_user_text(msgs)

        if "clasificador de intents" in sys_text:
            return _mk_response(json.dumps(self._classify_text(last)))

        if "detector de crisis" in sys_text:
            t = last.lower()
            is_crisis = "dolor de pecho" in t or "no puedo respirar" in t
            payload = {
                "is_crisis": is_crisis,
                "category": "urgencia_medica_aguda" if is_crisis else None,
                "severity": 5 if is_crisis else 1,
                "suggested_action": "Llamar 911 y trasladar a urgencias." if is_crisis else "",
            }
            return _mk_response(json.dumps(payload))

        # Default chat/SOUL reply.
        return _mk_response("Hola, soy Iris. ¿En qué te puedo ayudar?")


@pytest.fixture
def fake_anthropic(monkeypatch) -> FakeAnthropicClient:
    fake = FakeAnthropicClient()
    from iris_brain import chat as chat_mod
    from iris_brain import intents as intents_mod

    monkeypatch.setattr(chat_mod, "_client", fake)
    monkeypatch.setattr(intents_mod, "_client", fake)
    # safety ya no usa Anthropic — es keyword-based determinístico (HC port).
    return fake


# ---------------------------------------------------------------------------
# Relay mock
# ---------------------------------------------------------------------------


class FakeRelay:
    def __init__(self) -> None:
        self.owner_calls: list[dict[str, Any]] = []
        self.contact_calls: list[dict[str, Any]] = []

    def send_to_owner(self, ticket: dict[str, Any]) -> dict[str, Any]:
        self.owner_calls.append(ticket)
        return {"ok": True, "status": 200}

    def send_to_contact(self, thread_id: int, body: str) -> dict[str, Any]:
        self.contact_calls.append({"thread_id": thread_id, "body": body})
        return {"ok": True, "status": 200}


@pytest.fixture
def fake_relay(monkeypatch) -> FakeRelay:
    fake = FakeRelay()
    from iris_brain import chat as chat_mod
    from iris_brain import relay as relay_mod
    from iris_brain import server as server_mod

    def _get() -> FakeRelay:
        return fake

    monkeypatch.setattr(relay_mod, "get_relay", _get)
    monkeypatch.setattr(chat_mod, "get_relay", _get)
    monkeypatch.setattr(server_mod, "get_relay", _get)
    return fake


# ---------------------------------------------------------------------------
# FastAPI TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def client(fake_anthropic, fake_relay) -> TestClient:
    from iris_brain.server import app

    return TestClient(app)
