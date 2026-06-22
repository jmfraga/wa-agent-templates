"""Clasificador proactivo: decide si Phoenix debe intervenir aunque nadie lo
mencione. Sólo aplica a grupos con mode='proactive'.

Pipeline:
  cooldown_check → classify_relevance (Haiku) → decisión + audit_log
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from . import anthropic_client, app_config
from .config import settings
from .db import get_session
from .models import Group


@dataclass
class ProactiveDecision:
    should_respond: bool
    confidence: float  # 0.0-1.0
    intent_kind: str  # "kb_factual" | "kb_correction" | "answer_clarification" | "off_topic" | "low_value" | "cooldown" | "error"
    reasoning: str
    blocked_by_cooldown: bool = False


_INTENT_KINDS = [
    "kb_factual",            # alguien preguntó algo cuya respuesta está en una KB del grupo
    "kb_correction",         # alguien dijo algo incorrecto/incompleto sobre tema del grupo
    "answer_clarification",  # alguien pidió aclarar algo previo de Phoenix o del grupo
    "useful_addition",       # Phoenix tiene un dato relevante que aporta al hilo
    "off_topic",             # no es tema del grupo / del SOUL de Phoenix
    "low_value",             # tema sí, pero la intervención no agregaría valor (saludo, charla casual, etc.)
]


def _cooldown_minutes() -> int:
    return app_config.get_int("proactive_cooldown_min", default=settings.proactive_cooldown_min)


def _threshold() -> float:
    return app_config.get_float("proactive_threshold", default=settings.proactive_threshold)


def _check_cooldown(group_id: int) -> tuple[bool, Optional[int]]:
    """Devuelve (in_cooldown, remaining_seconds)."""
    cooldown = timedelta(minutes=_cooldown_minutes())
    with get_session() as s:
        g = s.get(Group, group_id)
        if not g or g.last_proactive_at is None:
            return False, None
        elapsed = datetime.utcnow() - g.last_proactive_at
        if elapsed >= cooldown:
            return False, None
        remaining = int((cooldown - elapsed).total_seconds())
        return True, remaining


def _mark_proactive(group_id: int) -> None:
    with get_session() as s:
        g = s.get(Group, group_id)
        if g:
            g.last_proactive_at = datetime.utcnow()
            s.commit()


def _build_system(soul: str, kb_summary: Optional[str]) -> str:
    # NOTA prompt-caching: el system es el prefijo estable (instrucciones + SOUL +
    # KBs). El nombre del grupo NO va aquí — es contexto del turno y se inyecta en
    # messages[] (ver classify_relevance), para no romper el prefijo cacheable.
    # Mínimo cacheable de Haiku 4.5 = 4096 tok: el prefijo sólo cachea con SOUL grande.
    blocks = [
        "Eres el clasificador de relevancia proactiva para Phoenix, un agente "
        "conversacional que opera en grupos de WhatsApp. Tu único trabajo es decidir "
        "si Phoenix debe intervenir EN ESTE MENSAJE sin haber sido mencionado.\n\n",
        "═══════ SOUL DEL GRUPO (scope autorizado de Phoenix) ═══════\n",
        soul.strip(),
        "\n═══════════════════════════════════════════════════════════\n\n",
    ]
    if kb_summary:
        blocks.append("KBs suscritas al grupo (todo lo que esté ahí es scope on-topic):\n")
        blocks.append(kb_summary)
        blocks.append("\n\n")
    blocks.append(
        "REGLA PRINCIPAL: si el tema del mensaje cae dentro del SOUL o de las KBs "
        "listadas arriba, considéralo ON-TOPIC. No subdividas el SOUL en 'medular' vs "
        "'periférico' — todo lo que esté listado cuenta. Ejemplos: si el SOUL dice "
        "'productos, plataformas (Clientify), equipo', entonces preguntas sobre "
        "leads, equipo y plataformas son ON-TOPIC.\n\n"
        "Phoenix DEBE intervenir cuando ocurre AL MENOS UNO:\n"
        "1. Hay una pregunta on-topic cuya respuesta probable está en una KB o en el "
        "   SOUL — sobre todo si nadie la ha respondido todavía en la historia.\n"
        "2. Alguien afirma algo incorrecto/incompleto sobre un tema del SOUL.\n"
        "3. Alguien pide aclaración de algo que Phoenix dijo antes.\n"
        "4. Phoenix tiene un dato concreto, verificable y útil que aporta al hilo.\n\n"
        "Phoenix NO DEBE intervenir si:\n"
        "- El tema está claramente fuera del SOUL y de las KBs.\n"
        "- Es charla puramente social (saludos, chistes, anécdotas personales).\n"
        "- La pregunta ya fue respondida en la historia reciente.\n"
        "- El mensaje es tan ambiguo que la intervención podría malinterpretarse.\n\n"
        "Nota sobre guardrails del SOUL: si el SOUL prohíbe ciertas acciones "
        "(ej. 'no cotizar en firme'), eso NO significa que el tema sea off-topic. "
        "Significa que Phoenix debe responder con cautela (referir a la persona "
        "responsable, dar info aproximada con disclaimer, etc.), no que deba callar. "
        "Marca should_respond=true en esos casos y deja que el agente principal "
        "modular la respuesta.\n\n"
        "Después de evaluar, llama OBLIGATORIAMENTE al tool `decide`:\n"
        "- should_respond: true si crees que Phoenix puede aportar valor real ahora\n"
        "- confidence: 0.0-1.0 (qué tan seguro estás de tu veredicto)\n"
        "- intent_kind: la categoría que mejor describe el caso\n"
        "- reasoning: una oración en español explicando"
    )
    return "".join(blocks)


_DECIDE_TOOL = {
    "name": "decide",
    "description": "Veredicto del clasificador.",
    "input_schema": {
        "type": "object",
        "properties": {
            "should_respond": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "intent_kind": {"type": "string", "enum": _INTENT_KINDS},
            "reasoning": {"type": "string"},
        },
        "required": ["should_respond", "confidence", "intent_kind", "reasoning"],
    },
}


def _history_text(history: list[dict], max_msgs: int = 8) -> str:
    """Compacta historia a texto plano para que el clasificador la lea barato."""
    rows = history[-max_msgs:]
    lines = []
    for m in rows:
        role = m.get("role")
        content = m.get("content", "")
        if isinstance(content, list):
            # Tool-use/result blocks: ignorar para el clasificador
            continue
        prefix = "Phoenix:" if role == "assistant" else ""
        lines.append(f"{prefix} {content}".strip())
    return "\n".join(lines)


def classify_relevance(
    *,
    group_id: int,
    group_display_name: str,
    soul: str,
    kb_summary: Optional[str],
    history: list[dict],
    current_author: str,
    current_text: str,
) -> ProactiveDecision:
    """Decide intervención proactiva. Honra cooldown."""
    if not current_text.strip():
        return ProactiveDecision(False, 0.0, "low_value", "mensaje vacío")

    in_cd, remaining = _check_cooldown(group_id)
    if in_cd:
        return ProactiveDecision(
            should_respond=False,
            confidence=0.0,
            intent_kind="cooldown",
            reasoning=f"grupo en cooldown ({remaining}s restantes)",
            blocked_by_cooldown=True,
        )

    system = _build_system(soul, kb_summary)
    history_txt = _history_text(history)
    # El nombre del grupo va en el turno user (contexto), no en el prefijo del system.
    user_block = (
        f"Grupo: *{group_display_name}*\n\n"
        + (f"Historia reciente del grupo:\n{history_txt}\n---\n" if history_txt else "")
        + f"Mensaje actual de {current_author}:\n{current_text}"
    )

    client = anthropic_client.get_client()
    try:
        resp = client.messages.create(
            model=settings.model_proactive,
            max_tokens=300,
            system=system,
            tools=[_DECIDE_TOOL],
            tool_choice={"type": "tool", "name": "decide"},
            messages=[{"role": "user", "content": user_block}],
        )
    except Exception as e:  # noqa: BLE001
        return ProactiveDecision(False, 0.0, "error", f"classifier_exception: {e}")

    for blk in resp.content:
        if getattr(blk, "type", None) == "tool_use" and blk.name == "decide":
            args = blk.input or {}
            confidence = float(args.get("confidence", 0.0) or 0.0)
            should = bool(args.get("should_respond", False))
            intent = args.get("intent_kind", "low_value")
            reasoning = (args.get("reasoning", "") or "")[:500]
            # Threshold gate (incluso si el modelo dice should=True, exigimos confidence ≥ threshold).
            if should and confidence < _threshold():
                should = False
            return ProactiveDecision(should, confidence, intent, reasoning)

    return ProactiveDecision(False, 0.0, "error", "no_tool_use_in_response")


def mark_triggered(group_id: int) -> None:
    """Llamar después de publicar la intervención proactiva real (al final del loop)."""
    _mark_proactive(group_id)
