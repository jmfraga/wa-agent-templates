"""Clasificador de intent usando Haiku con system prompt cacheado."""
from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from .config import settings

log = logging.getLogger("iris_brain.intents")

INTENTS = [
    "consulta_cita",
    "info_curso",
    "info_asesoria",
    "seguimiento_paciente",
    "urgencia_clinica",
    "pago_facturacion",
    "saludo_smalltalk",
    "otro",
]

_SYSTEM = """Eres un clasificador de intents para Iris, asistente del owner por WhatsApp.

Tu única tarea: leer el mensaje del contacto y devolver un JSON con la categoría más probable.

Categorías:
- consulta_cita: quiere agendar/reagendar/cancelar consulta médica.
- info_curso: pregunta por cursos (fechas, contenido, sede, modalidad). NO precio final.
- info_asesoria: pide asesoría profesional/académica (no clínica).
- seguimiento_paciente: paciente conocido reportando evolución, resultados, dudas tras consulta.
- urgencia_clinica: síntoma agudo, dolor severo, fiebre alta, sangrado, deterioro. Lo escala safety.
- pago_facturacion: pregunta por pago, factura, datos fiscales, cuenta.
- saludo_smalltalk: hola, gracias, despedidas, sin contenido accionable.
- otro: nada de lo anterior.

Responde SOLO JSON: {"intent": "<categoria>", "confidence": 0.0-1.0, "reasoning": "1 frase"}.
Nada de texto adicional."""

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def classify_intent(text: str, history: list[dict] | None = None) -> dict[str, Any]:
    """Devuelve {intent, confidence, reasoning}. Tolerante a fallos."""
    msgs: list[dict] = []
    if history:
        # Últimos 4 turnos como contexto.
        msgs.extend(history[-4:])
    msgs.append({"role": "user", "content": text})

    try:
        resp = _get_client().messages.create(
            model=settings.IRIS_BRAIN_MODEL_DEFAULT,
            max_tokens=200,
            # NOTA: _SYSTEM (~250 tok) está bajo el mínimo cacheable de Haiku 4.5 (4096),
            # así que no marcamos cache_control — sería no-op (cache_creation=0 sin error).
            # Si en el futuro _SYSTEM crece >4096, reañadir cache_control.
            system=_SYSTEM,
            messages=msgs,
        )
    except anthropic.APIError as e:
        log.exception("classify_intent API error")
        return {"intent": "otro", "confidence": 0.0, "reasoning": f"api_error: {e}"}

    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    # Tolerar fences/ruido.
    raw = raw.strip("`")
    if raw.startswith("json"):
        raw = raw[4:].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("classify_intent JSON inválido: %r", raw)
        return {"intent": "otro", "confidence": 0.0, "reasoning": "json_decode_fail"}

    intent = data.get("intent", "otro")
    if intent not in INTENTS:
        intent = "otro"
    return {
        "intent": intent,
        "confidence": float(data.get("confidence", 0.0)),
        "reasoning": data.get("reasoning", ""),
    }
