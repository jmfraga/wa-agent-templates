"""Render Telegram messages and inline keyboards for relay tickets."""
from __future__ import annotations

from typing import Any, Optional

# Icon per ticket kind (mirrors docs/04-intent-taxonomy.md).
KIND_ICONS: dict[str, str] = {
    "urgencia_clinica": "🩺",
    "consulta_cita": "📅",
    "info_curso": "🎓",
    "info_asesoria": "💼",
    "pago_facturacion": "💸",
    "seguimiento_paciente": "📋",
    "saludo_smalltalk": "📩",
    "otro": "📩",
}

KIND_LABELS: dict[str, str] = {
    "urgencia_clinica": "Urgencia clínica",
    "consulta_cita": "Consulta / cita",
    "info_curso": "Curso",
    "info_asesoria": "Asesoría",
    "pago_facturacion": "Pago / facturación",
    "seguimiento_paciente": "Seguimiento",
    "saludo_smalltalk": "Saludo",
    "otro": "Otro",
}


def _esc(text: Optional[str]) -> str:
    """Telegram HTML escaping (we use parse_mode=HTML)."""
    if text is None:
        return ""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def render_ticket_message(payload: dict[str, Any]) -> str:
    """Build the HTML body shown to Owner when a new ticket arrives."""
    kind = payload.get("kind", "otro")
    icon = KIND_ICONS.get(kind, "📩")
    label = KIND_LABELS.get(kind, kind)
    ticket_id = payload.get("ticket_id")
    thread_id = payload.get("thread_id")
    summary = payload.get("summary", "(sin resumen)")
    draft = payload.get("draft")
    contact_name = payload.get("contact_name") or "(sin nombre)"
    contact_phone = payload.get("contact_phone") or "(sin teléfono)"
    urgent = bool(payload.get("urgent"))

    lines: list[str] = []
    header = f"{icon} <b>{_esc(label)}</b> · ticket #{ticket_id}"
    if urgent:
        header = f"🚨 <b>URGENTE</b> — {header}"
    lines.append(header)
    lines.append("")
    lines.append(f"<b>De:</b> {_esc(contact_name)} (<code>{_esc(contact_phone)}</code>)")
    if thread_id is not None:
        lines.append(f"<b>Thread:</b> #{thread_id}")
    lines.append("")
    lines.append(f"<b>Resumen:</b>\n{_esc(summary)}")

    if draft:
        lines.append("")
        lines.append("<b>Plantilla propuesta:</b>")
        lines.append(f"<blockquote>{_esc(draft)}</blockquote>")

    return "\n".join(lines)


def render_urgent_banner(payload: dict[str, Any]) -> str:
    contact_name = payload.get("contact_name") or "(sin nombre)"
    contact_phone = payload.get("contact_phone") or ""
    return (
        "🚨 <b>URGENTE</b> 🚨\n"
        f"Mensaje urgente de <b>{_esc(contact_name)}</b> "
        f"(<code>{_esc(contact_phone)}</code>)\n"
        "Revisa el ticket de arriba lo antes posible."
    )


def build_inline_keyboard(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the reply_markup dict for sendMessage."""
    ticket_id = payload.get("ticket_id")
    thread_id = payload.get("thread_id")
    draft = payload.get("draft")
    urgent = bool(payload.get("urgent"))

    row1: list[dict[str, str]] = []
    if draft:
        row1.append({
            "text": "✅ Aprobar plantilla",
            "callback_data": f"approve:{ticket_id}",
        })
    row1.append({
        "text": "✍️ Responder",
        "callback_data": f"reply:{ticket_id}",
    })

    row2: list[dict[str, str]] = []
    # Disable reject button when urgent (force human decision via reply/approve).
    if not urgent:
        row2.append({
            "text": "🚫 Rechazar/Cerrar",
            "callback_data": f"close:{ticket_id}",
        })
    if thread_id is not None:
        row2.append({
            "text": "📋 Ver thread",
            "callback_data": f"thread:{ticket_id}:{thread_id}",
        })

    rows = [r for r in (row1, row2) if r]
    return {"inline_keyboard": rows}


def render_closed(ticket_id: int) -> str:
    return f"🚫 <b>Cerrado por Owner</b> · ticket #{ticket_id}"


def render_reply_prompt(ticket_id: int) -> str:
    return (
        f"✍️ <b>Ticket #{ticket_id}</b> · esperando tu respuesta\n\n"
        f"Solo escribe tu mensaje y enviarlo aquí — yo se lo mando al usuario.\n"
        f"(También puedes usar <i>Reply</i> nativo a este mensaje si prefieres.)"
    )


def render_reply_sent(ticket_id: int, body: str) -> str:
    preview = body if len(body) <= 400 else body[:400] + "…"
    return (
        f"✓ <b>Respuesta enviada</b> · ticket #{ticket_id}\n"
        f"<blockquote>{_esc(preview)}</blockquote>"
    )


def render_approved(ticket_id: int, draft: str) -> str:
    preview = draft if len(draft) <= 400 else draft[:400] + "…"
    return (
        f"✓ <b>Plantilla aprobada</b> · ticket #{ticket_id}\n"
        f"<blockquote>{_esc(preview)}</blockquote>"
    )


def build_user_question_keyboard(contact_phone: str) -> dict[str, Any]:
    """Keyboard para report_to_owner cuando viene contact_phone (Feature 1).

    Callbacks: usr:reply / usr:later / usr:no_info / usr:silence / usr:close.
    """
    p = contact_phone
    return {
        "inline_keyboard": [
            [{"text": "💬 Responder con texto", "callback_data": f"usr:reply:{p}"}],
            [{"text": "📅 Te confirmo más tarde", "callback_data": f"usr:later:{p}"}],
            [{"text": "🙅 No tengo info", "callback_data": f"usr:no_info:{p}"}],
            [{"text": "🔇 No responder", "callback_data": f"usr:silence:{p}"}],
            [{"text": "✅ Cerrar conversación", "callback_data": f"usr:close:{p}"}],
        ]
    }


def render_user_question(text: str, contact_phone: str, task_id: int | None = None) -> str:
    head = f"🤖 <b>Iris</b> · task #{task_id}\n" if task_id else "🤖 <b>Iris</b>\n"
    foot = f"\n<i>contacto:</i> <code>{_esc(contact_phone)}</code>"
    return head + text + foot


def build_plan_keyboard(task_id: int) -> dict[str, Any]:
    """Keyboard para report_plan_to_owner (Feature 2)."""
    return {
        "inline_keyboard": [
            [{"text": "✅ Enviar ahora", "callback_data": f"plan:send:{task_id}"}],
            [{"text": "📅 Programar para…", "callback_data": f"plan:schedule:{task_id}"}],
            [{"text": "✏️ Cambiar texto", "callback_data": f"plan:edit:{task_id}"}],
            [{"text": "❌ Cancelar", "callback_data": f"plan:cancel:{task_id}"}],
        ]
    }


def build_plan_schedule_submenu(task_id: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "Mañana 9am", "callback_data": f"plan:sched_pick:{task_id}:tomorrow_9"}],
            [{"text": "Lun 9am", "callback_data": f"plan:sched_pick:{task_id}:mon_9"}],
            [{"text": "Mar 9am", "callback_data": f"plan:sched_pick:{task_id}:tue_9"}],
            [{"text": "Vie 9am", "callback_data": f"plan:sched_pick:{task_id}:fri_9"}],
            [{"text": "⏰ Custom…", "callback_data": f"plan:sched_pick:{task_id}:custom"}],
            [{"text": "← Volver", "callback_data": f"plan:back:{task_id}"}],
        ]
    }


def render_plan_message(task_id: int, summary: str, plan_text: str) -> str:
    return (
        f"🎯 <b>Plan listo</b> · task #{task_id}\n\n"
        f"{_esc(summary)}\n"
        f"─────────\n"
        f"{plan_text}"
    )


def build_iris_panel_keyboard() -> dict[str, Any]:
    """Keyboard del comando /iris (Feature 3)."""
    return {
        "inline_keyboard": [
            [{"text": "🔇 Pausar Iris 24h", "callback_data": "iris:pause:24h"}],
            [{"text": "🔇 Pausar Iris 7 días", "callback_data": "iris:pause:7d"}],
            [{"text": "▶️ Reactivar Iris", "callback_data": "iris:resume"}],
            [{"text": "📊 Conversaciones activas", "callback_data": "iris:list_active"}],
            [{"text": "🔇 Silenciar contacto…", "callback_data": "iris:silence_contact"}],
            [{"text": "⚙️ Modo silencioso global", "callback_data": "iris:silent_mode_toggle"}],
        ]
    }


def render_iris_panel(status: dict[str, Any]) -> str:
    paused = status.get("paused_until")
    silent_global = bool(status.get("silent_mode_global"))
    lines = ["🤖 <b>Control de Iris</b>", "─────────"]
    if paused:
        lines.append(f"⏸ Pausada hasta: <code>{_esc(str(paused))}</code>")
    else:
        lines.append("▶️ Activa")
    lines.append(f"🔕 Modo silencioso global: {'ON' if silent_global else 'OFF'}")
    return "\n".join(lines)


def render_thread_messages(thread_id: int, messages: list[dict[str, Any]]) -> str:
    if not messages:
        return f"📋 Thread #{thread_id}: (sin mensajes)"
    lines = [f"📋 <b>Thread #{thread_id}</b> · últimos {len(messages)} mensajes", ""]
    for m in messages:
        direction = m.get("direction", "?")
        arrow = "←" if direction == "in" else "→"
        ts = m.get("ts", "")
        body = _esc(m.get("body", ""))
        lines.append(f"<b>{arrow} {direction}</b> <i>{_esc(str(ts))}</i>\n{body}")
        lines.append("")
    return "\n".join(lines).rstrip()
