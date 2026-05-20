"""Orquestador: pipeline message-in → reply.

Pipeline:
  1. Upsert contact (por phone)
  2. Abrir/recuperar thread
  3. Insertar message in
  4. classify_intent (Haiku)
  5. Si urgencia_clinica → detect_crisis (Sonnet)
  6. Si crisis → respuesta inmediata + ping owner urgente
  7. Si intent requiere owner → open_ticket + respuesta puente
  8. Si info_curso → consulta kb_facts; si hay, responde; si no, ticket
  9. Otros → respuesta directa con SOUL via Haiku + tool-use loop (max 5)
"""
from __future__ import annotations

import logging
from typing import Any

import anthropic

from . import sessions, soul, tools
from .config import settings
from .db import get_session
from .models import Contact, MessageDirection
from .relay import get_relay

log = logging.getLogger("iris_brain.chat")

MAX_TOOL_ITERATIONS = 5

# Intents que siempre escalan a owner.
ESCALATE_INTENTS = {"consulta_cita", "info_asesoria", "seguimiento_paciente", "pago_facturacion"}

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    return _client


def _system_blocks(contact_id: int | None = None, thread_id: int | None = None) -> list[dict]:
    """SOUL cacheado + (opcional) bloque dinámico con ficha del contacto actual."""
    blocks: list[dict] = [soul.load_soul()]
    if contact_id is None:
        return blocks
    with get_session() as s:
        c = s.get(Contact, contact_id)
        if c is None:
            return blocks
        kind_val = c.kind.value if c.kind else "otro"
        notes_preview = (c.notes or "").strip()
        if len(notes_preview) > 600:
            notes_preview = notes_preview[-600:]
        lines = [
            f"## Contacto en este thread",
            f"- phone: `{c.phone}`",
            f"- name: {c.name if c.name else '(sin nombre — pregúntalo en este turno o el siguiente)'}",
            f"- kind: {kind_val}",
            f"- notes:\n{notes_preview if notes_preview else '(vacío)'}",
        ]
        if thread_id is not None:
            lines.insert(1, f"- **thread_id: {thread_id}** (úsalo SIEMPRE así en open_ticket, NO uses el phone)")
        # contact_id es necesario para tools agentic cuando owner habla (owner_id en create_task).
        lines.insert(1, f"- **contact_id: {c.id}** (úsalo como owner_id en create_task si eres modo owner)")
        if not c.name:
            lines.append(
                "\n**IMPORTANTE:** este contacto aún no tiene nombre registrado. "
                "Si en este mensaje menciona su nombre (o lo deduces con alta confianza), "
                "DEBES llamar `update_contact(phone, name=...)` ANTES de generar tu respuesta. "
                "Si no se ha presentado todavía, pregúntale el nombre en tu respuesta de forma natural."
            )
        if kind_val == "otro":
            lines.append(
                "\nSi en este turno queda claro si es paciente, prospecto_curso o asesoria, "
                "actualiza con `update_contact(phone, kind=...)`."
            )
        blocks.append({"type": "text", "text": "\n".join(lines)})
    return blocks


def _extract_text(content_blocks) -> str:
    return "".join(
        b.text for b in content_blocks if getattr(b, "type", None) == "text"
    ).strip()


def _contact_card(contact_id: int) -> tuple[str | None, str | None]:
    """Devuelve (phone, name) del contacto o (None, None) si no existe."""
    with get_session() as s:
        c = s.get(Contact, contact_id)
        if c is None:
            return None, None
        return c.phone, c.name


def _direct_reply(messages: list[dict], contact_id: int | None = None, thread_id: int | None = None) -> tuple[str, dict, str]:
    """Llama Haiku con tools y devuelve (reply_text, usage, stop_reason)."""
    client = _get_client()
    kwargs: dict[str, Any] = {
        "model": settings.IRIS_BRAIN_MODEL_DEFAULT,
        "max_tokens": settings.IRIS_BRAIN_MAX_TOKENS,
        "system": _system_blocks(contact_id, thread_id),
        "tools": tools.TOOLS,
    }
    if settings.IRIS_BRAIN_THINKING == "adaptive":
        kwargs["thinking"] = {"type": "adaptive"}
    if settings.IRIS_BRAIN_EFFORT:
        kwargs["output_config"] = {"effort": settings.IRIS_BRAIN_EFFORT}

    usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    response = None
    # Acumula texto de TODAS las iteraciones. Iris a veces escribe texto en el mismo
    # turno donde llama tools — antes se perdía porque solo capturábamos la última respuesta.
    text_chunks: list[str] = []

    for _ in range(MAX_TOOL_ITERATIONS):
        response = client.messages.create(messages=messages, **kwargs)
        u = getattr(response, "usage", None)
        if u:
            usage["input_tokens"] += getattr(u, "input_tokens", 0)
            usage["output_tokens"] += getattr(u, "output_tokens", 0)
            usage["cache_read_input_tokens"] += getattr(u, "cache_read_input_tokens", 0)
            usage["cache_creation_input_tokens"] += getattr(u, "cache_creation_input_tokens", 0)
        # Captura texto de ESTE turno (puede coexistir con tool_use).
        chunk = _extract_text(response.content)
        if chunk:
            text_chunks.append(chunk)
        if response.stop_reason != "tool_use":
            break
        tool_uses = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for tu in tool_uses:
            log.info("tool: %s args=%s", tu.name, tu.input)
            r = tools.execute(tu.name, tu.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": tools.to_text(r),
                "is_error": "error" in r,
            })
        messages.append({"role": "user", "content": results})
    else:
        log.warning("max tool iterations reached")

    reply = "\n\n".join(text_chunks).strip()
    stop = response.stop_reason if response else "error"

    # Safety net: si TODAS las iteraciones produjeron 0 texto, fuerza una última.
    # Con la nueva acumulación esto debería ser MUY raro.
    if not reply and response and stop in {"end_turn", "tool_use"} and len(messages) > 0:
        try:
            kwargs_no_tools = {k: v for k, v in kwargs.items() if k != "tools"}
            forced_messages = messages + [{
                "role": "user",
                "content": "Genera ahora tu respuesta de texto visible al paciente, en 1-3 líneas, español mexicano, tono Iris.",
            }]
            forced = client.messages.create(messages=forced_messages, **kwargs_no_tools)
            reply = _extract_text(forced.content)
            stop = forced.stop_reason
            log.warning("forced text-only follow-up; reply_len=%d (SOUL rule violada?)", len(reply))
        except Exception:  # noqa: BLE001
            log.exception("forced follow-up failed")

    return reply, usage, stop


def handle_message(
    contact_phone: str,
    text: str,
    media_url: str | None = None,
    pushname: str | None = None,
) -> dict[str, Any]:
    text = (text or "").strip()
    if not text and not media_url:
        return {"error": "mensaje vacío"}

    # 1-3. Persistir entrada.
    contact_id = sessions.upsert_contact(contact_phone)
    thread_id = sessions.open_or_get_thread(contact_id)
    sessions.append_message(thread_id, MessageDirection.in_, text, media_url=media_url)

    # Si recibimos pushname (WA display name) y el contacto no tiene name, úsalo como placeholder.
    if pushname:
        with get_session() as s:
            c = s.get(Contact, contact_id)
            if c is not None and not c.name:
                c.name = pushname
                log.info("contact %s name set from pushname=%r", contact_id, pushname)
    history = sessions.history_for_model(thread_id, max_turns=settings.IRIS_BRAIN_MAX_HISTORY)

    reply = ""
    ticket_id: int | None = None
    safety_result: dict | None = None
    intent: str = "otro"
    intent_confidence: float = 0.0
    model_used = settings.IRIS_BRAIN_MODEL_DEFAULT
    cphone, cname = _contact_card(contact_id)

    # 3.5 Modo owner — owner dicta instrucciones (agéntico). Bypass safety + intent classify.
    # Iris responde con tools agentic (search_contacts, create_task, send_outbound, etc).
    contact_kind = None
    with get_session() as s:
        c = s.get(Contact, contact_id)
        if c is not None:
            contact_kind = c.kind.value if c.kind else None
    if contact_kind == "owner":
        intent = "owner_instruction"
        intent_confidence = 1.0
        msgs = list(history)
        reply, usage_out, _stop = _direct_reply(msgs, contact_id=contact_id, thread_id=thread_id)
        # Hard fallback (owner no debería quedarse sin respuesta — confunde el flujo agéntico).
        if not reply:
            reply = "Sí, te leí. (Sin respuesta de texto generada — revisa logs si esto se repite.)"
        sessions.append_message(
            thread_id, MessageDirection.out, reply, model_used=model_used,
            tokens_input=(usage_out or {}).get("input_tokens"),
            tokens_output=(usage_out or {}).get("output_tokens"),
        )
        return {
            "ok": True,
            "thread_id": thread_id,
            "contact_id": contact_id,
            "intent": intent,
            "intent_confidence": intent_confidence,
            "safety": None,
            "ticket_id": None,
            "reply": reply,
            "model": model_used,
        }

    # 4. Safety first — detector keyword-based (portado de HC Sprint 1).
    # Corre ANTES que classify_intent: es determinístico, sin costo LLM, y si dispara
    # bypassa la clasificación normal con respuesta template + ticket urgente.
    from . import safety
    crisis = safety.detect(text)
    if crisis is not None:
        intent = "urgencia_clinica"
        model_used = "safety_keyword"
        safety_result = {
            "category": crisis.category,
            "level": crisis.level,
            "matched": crisis.matched,
        }
        reply = safety.get_response(crisis)
        is_high = crisis.level == "high"
        tid = tools._open_ticket(
            thread_id,
            kind="urgencia" if is_high else "posible_urgencia",
            summary=f"{'🚨 ' if is_high else ''}{crisis.category.upper()} [{crisis.level}]: {text[:180]}",
            draft_for_owner=f"Match: '{crisis.matched}'\n\nTexto del paciente:\n{text}",
        )
        ticket_id = tid.get("ticket_id")
        get_relay().send_to_owner({
            "id": ticket_id,
            "thread_id": thread_id,
            "kind": "urgencia" if is_high else "posible_urgencia",
            "summary": f"{crisis.category} [{crisis.level}]",
            "draft_for_owner": f"Match keyword: '{crisis.matched}'\nNivel: {crisis.level}\n\nTexto:\n{text}",
            "urgent": is_high,
            "contact_phone": cphone,
            "contact_name": cname,
        })
        log.warning("CRISIS detected cat=%s level=%s phone=%s", crisis.category, crisis.level, contact_phone)
        # Persistir y salir — NO se llama al LLM ni a classify_intent.
        sessions.append_message(thread_id, MessageDirection.out, reply, model_used=model_used)
        return {
            "ok": True,
            "thread_id": thread_id,
            "contact_id": contact_id,
            "intent": intent,
            "intent_confidence": 1.0,
            "safety": safety_result,
            "ticket_id": ticket_id,
            "reply": reply,
            "model": model_used,
        }

    # 4.5 Response tracking — si este contacto tiene un task_target.status='sent',
    # esta entrada es la respuesta al outbound de Iris, NO una nueva consulta.
    # Clasificar, registrar, ping al owner. NO abrir ticket.
    from . import agentic
    active_tt = agentic.find_active_task_target(contact_id)
    if active_tt is not None:
        cls = agentic.classify_response(active_tt["message_sent"] or "", text)
        agentic.classify_and_record_response(active_tt["id"], text, cls["classification"])
        # Reportar a owner en vivo
        emoji = {
            "accepted": "✅", "declined": "❌", "maybe": "🤔",
            "clarify": "❓", "other": "💬",
        }.get(cls["classification"], "💬")
        cname_display = cname or cphone or "(contacto)"
        report = (
            f"{emoji} <b>{cname_display}</b> respondió ({cls['classification']}) a task #{active_tt['task_id']}:\n"
            f"<blockquote>{text[:300]}</blockquote>"
        )
        agentic.report_to_owner(report, task_id=active_tt["task_id"])
        # Reply al contacto con un breve acknowledgment
        ack = {
            "accepted": "Perfecto, le aviso al doctor. 🙏",
            "declined": "Entendido, le paso el mensaje al doctor.",
            "maybe": "Va, le aviso al doctor que estás viendo. Quedo al pendiente.",
            "clarify": "Le paso tu duda al doctor y te confirmo.",
            "other": "Gracias, le aviso al doctor.",
        }[cls["classification"]]
        sessions.append_message(thread_id, MessageDirection.out, ack, model_used="task_response_ack")
        log.info(
            "response tracking: contact_id=%s target_id=%s cls=%s",
            contact_id, active_tt["id"], cls["classification"],
        )
        return {
            "ok": True,
            "thread_id": thread_id,
            "contact_id": contact_id,
            "intent": "task_response",
            "intent_confidence": 1.0,
            "safety": None,
            "ticket_id": None,
            "reply": ack,
            "model": "task_response_pipeline",
            "task_id": active_tt["task_id"],
            "task_target_id": active_tt["id"],
            "classification": cls["classification"],
        }

    # 5. Clasificar intent (solo si no hubo crisis ni task_response).
    from . import intents
    intent_result = intents.classify_intent(text, history=history)
    intent = intent_result["intent"]
    intent_confidence = intent_result["confidence"]
    log.info("intent=%s conf=%.2f phone=%s", intent, intent_confidence, contact_phone)

    # 7. Intents que escalan a owner: abrimos ticket programáticamente y
    # dejamos que Iris genere una respuesta personalizada usando el contexto.
    usage_out: dict | None = None
    if intent in ESCALATE_INTENTS:
        tid = tools._open_ticket(
            thread_id,
            kind=intent,
            summary=text[:200],
            draft_for_owner=None,
        )
        ticket_id = tid.get("ticket_id")
        get_relay().send_to_owner({
            "id": ticket_id,
            "thread_id": thread_id,
            "kind": intent,
            "summary": text[:200],
            "draft_for_owner": None,
            "urgent": False,
            "contact_phone": cphone,
            "contact_name": cname,
        })
        # Generar respuesta puente personalizada con el LLM (no template fijo).
        # Inyectamos al final del historial una nota de sistema indicando que el ticket ya
        # se abrió, para que Iris no llame open_ticket de nuevo y solo redacte el puente.
        msgs = list(history)
        if msgs and msgs[-1]["role"] == "user":
            msgs[-1] = {
                **msgs[-1],
                "content": (
                    f"{msgs[-1]['content']}\n\n"
                    f"[nota interna del sistema, NO la repitas: ya abrí el ticket #{ticket_id} "
                    f"de tipo {intent} con owner. Solo responde al paciente con una frase puente "
                    f"breve (1-2 líneas), personalizada con su nombre si lo sabes, en tono cálido "
                    f"de Iris. Ejemplos: 'Gracias {{nombre}}, déjame checar con el doctor', "
                    f"'Va, paso el contexto al Dr. Owner y te confirmo en cuanto sepa', etc. Varía.]"
                ),
            }
        reply, usage_out, _stop = _direct_reply(msgs, contact_id=contact_id, thread_id=thread_id)

    # 8. info_curso: dejamos que el modelo use lookup_kb_fact; si no encuentra, ticket.
    if intent == "info_curso":
        msgs = list(history)
        reply, usage_out, stop = _direct_reply(msgs, contact_id=contact_id, thread_id=thread_id)
        if not reply or stop == "max_tokens":
            tid = tools._open_ticket(
                thread_id,
                kind="info_curso",
                summary=text[:200],
                draft_for_owner=None,
            )
            ticket_id = tid.get("ticket_id")
            reply = reply or "Déjame confirmar los detalles con el doctor y te aviso en cuanto sepa."

    # 9. saludo_smalltalk / otro: respuesta directa.
    elif intent not in ESCALATE_INTENTS:
        msgs = list(history)
        reply, usage_out, _stop = _direct_reply(msgs, contact_id=contact_id, thread_id=thread_id)

    # Hard fallback: si después de todo no hay reply, manda un ack genérico
    # para que el paciente NO se quede sin respuesta.
    if not reply and intent not in {"urgencia_clinica"}:
        reply = "Gracias, recibí tu mensaje. Te respondo en breve."
        log.warning("hard fallback reply used for intent=%s phone=%s", intent, contact_phone)

    # Persistir respuesta saliente.
    if reply:
        sessions.append_message(
            thread_id,
            MessageDirection.out,
            reply,
            model_used=model_used,
            tokens_input=(usage_out or {}).get("input_tokens"),
            tokens_output=(usage_out or {}).get("output_tokens"),
        )

    return {
        "ok": True,
        "thread_id": thread_id,
        "contact_id": contact_id,
        "intent": intent,
        "intent_confidence": intent_confidence,
        "safety": safety_result,
        "ticket_id": ticket_id,
        "reply": reply,
        "model": model_used,
    }
