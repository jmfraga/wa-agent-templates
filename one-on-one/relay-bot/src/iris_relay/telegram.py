"""Telegram client + polling loop using plain HTTPS (no python-telegram-bot).

Why requests-based: FastAPI is sync-friendly and we want a dead-simple background
thread doing long-polling getUpdates. Mixing python-telegram-bot's asyncio
Application with uvicorn's loop is more pain than it's worth for this scope.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

import requests

from .config import Settings
from .state import StateStore
from .templates import (
    KIND_ICONS,
    _esc,
    build_inline_keyboard,
    render_approved,
    render_closed,
    render_reply_prompt,
    render_reply_sent,
    render_thread_messages,
    render_ticket_message,
    render_urgent_banner,
)

log = logging.getLogger("iris_relay.telegram")


class TelegramClient:
    """Minimal Telegram Bot API client (sendMessage / editMessageText / getUpdates)."""

    def __init__(self, settings: Settings, http: Optional[requests.Session] = None):
        self.settings = settings
        self.http = http or requests.Session()
        self.timeout = settings.http_timeout

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self.settings.telegram_api_base}/{method}"
        r = self.http.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error on {method}: {data}")
        return data.get("result", {})

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
        reply_to_message_id: Optional[int] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = reply_to_message_id
        return self._post("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._post("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._post("answerCallbackQuery", payload)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 25) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        url = f"{self.settings.telegram_api_base}/getUpdates"
        r = self.http.post(url, json=payload, timeout=timeout + 10)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram getUpdates error: {data}")
        return data.get("result", [])


class BrainClient:
    """HTTP client for the Iris brain (POST /jmf/reply, POST /tickets/{id}/close, etc.)."""

    def __init__(self, settings: Settings, http: Optional[requests.Session] = None):
        self.settings = settings
        self.http = http or requests.Session()
        self.timeout = settings.http_timeout

    def jmf_reply(self, ticket_id: int, body: str) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/jmf/reply"
        r = self.http.post(url, json={"ticket_id": ticket_id, "body": body}, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def close_ticket(self, ticket_id: int) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/tickets/{ticket_id}/close"
        r = self.http.post(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def thread_messages(self, thread_id: int, limit: int = 5) -> list[dict[str, Any]]:
        url = f"{self.settings.brain_url.rstrip('/')}/threads/{thread_id}/messages"
        r = self.http.get(url, params={"limit": limit}, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return data.get("messages", data) if isinstance(data, dict) else data

    def _admin_headers(self) -> dict[str, str]:
        return {"X-Iris-Admin-Token": self.settings.iris_admin_token}

    def admin_tickets_live(self) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/admin/tickets/live"
        r = self.http.get(url, headers=self._admin_headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def admin_metrics_today(self) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/admin/metrics/today"
        r = self.http.get(url, headers=self._admin_headers(), timeout=self.timeout)
        r.raise_for_status()
        return r.json()


class TelegramRelay:
    """Orchestrates ticket send + callbacks + reply correlation.

    Spawns a background daemon thread doing getUpdates long-polling.
    """

    def __init__(
        self,
        settings: Settings,
        state: StateStore,
        telegram: Optional[TelegramClient] = None,
        brain: Optional[BrainClient] = None,
    ):
        self.settings = settings
        self.state = state
        self.telegram = telegram or TelegramClient(settings)
        self.brain = brain or BrainClient(settings)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Cache draft per ticket so "Aprobar plantilla" works without re-asking brain.
        self._draft_cache: dict[int, str] = {}

    # --- Outbound ------------------------------------------------------

    def send_ticket(self, payload: dict[str, Any]) -> dict[str, Any]:
        chat_id = self.settings.telegram_chat_id
        text = render_ticket_message(payload)
        markup = build_inline_keyboard(payload)
        sent = self.telegram.send_message(chat_id, text, reply_markup=markup)
        message_id = sent.get("message_id")
        ticket_id = int(payload["ticket_id"])
        self.state.upsert_ticket(
            ticket_id=ticket_id,
            chat_id=int(sent.get("chat", {}).get("id", chat_id) or 0),
            message_id=int(message_id),
            thread_id=payload.get("thread_id"),
            kind=payload.get("kind"),
            status="awaiting_jmf",
        )
        if payload.get("draft"):
            self._draft_cache[ticket_id] = payload["draft"]
        if payload.get("urgent"):
            self.telegram.send_message(chat_id, render_urgent_banner(payload))
        return {"telegram_message_id": message_id, "ticket_id": ticket_id}

    # --- Polling loop --------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.settings.telegram_bot_token:
            log.warning("TELEGRAM_BOT_TOKEN unset — skipping polling thread.")
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="iris-relay-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _poll_loop(self) -> None:
        offset: Optional[int] = None
        while not self._stop.is_set():
            try:
                updates = self.telegram.get_updates(offset=offset, timeout=25)
                for upd in updates:
                    offset = upd["update_id"] + 1
                    try:
                        self._handle_update(upd)
                    except Exception:  # noqa: BLE001
                        log.exception("Failed to handle update %s", upd.get("update_id"))
            except Exception:  # noqa: BLE001
                log.exception("Telegram getUpdates failed; backing off")
                time.sleep(max(self.settings.telegram_poll_interval, 5.0))

    # --- Inbound dispatch ----------------------------------------------

    def _handle_update(self, upd: dict[str, Any]) -> None:
        if "callback_query" in upd:
            self._handle_callback(upd["callback_query"])
        elif "message" in upd:
            self._handle_message(upd["message"])

    def _handle_callback(self, cb: dict[str, Any]) -> None:
        data = cb.get("data", "")
        cb_id = cb["id"]
        msg = cb.get("message", {})
        chat_id = msg.get("chat", {}).get("id")
        message_id = msg.get("message_id")
        parts = data.split(":")
        action = parts[0] if parts else ""

        try:
            if action == "approve" and len(parts) >= 2:
                ticket_id = int(parts[1])
                draft = self._draft_cache.get(ticket_id)
                if not draft:
                    self.telegram.answer_callback_query(cb_id, "Sin plantilla en caché")
                    return
                self.brain.jmf_reply(ticket_id, draft)
                self.state.set_status(ticket_id, "closed")
                self.telegram.edit_message_text(chat_id, message_id, render_approved(ticket_id, draft))
                self.telegram.answer_callback_query(cb_id, "Enviado")

            elif action == "reply" and len(parts) >= 2:
                ticket_id = int(parts[1])
                self.state.set_status(ticket_id, "awaiting_reply")
                self.telegram.edit_message_text(chat_id, message_id, render_reply_prompt(ticket_id))
                self.telegram.answer_callback_query(cb_id, "Responde con reply")

            elif action == "close" and len(parts) >= 2:
                ticket_id = int(parts[1])
                self.brain.close_ticket(ticket_id)
                self.state.set_status(ticket_id, "closed")
                self.telegram.edit_message_text(chat_id, message_id, render_closed(ticket_id))
                self.telegram.answer_callback_query(cb_id, "Cerrado")

            elif action == "thread" and len(parts) >= 3:
                thread_id = int(parts[2])
                messages = self.brain.thread_messages(thread_id, limit=5)
                self.telegram.send_message(chat_id, render_thread_messages(thread_id, messages))
                self.telegram.answer_callback_query(cb_id)

            else:
                self.telegram.answer_callback_query(cb_id, "Acción desconocida")
        except Exception as e:  # noqa: BLE001
            log.exception("Callback handling failed")
            try:
                self.telegram.answer_callback_query(cb_id, f"Error: {e}")
            except Exception:  # noqa: BLE001
                pass

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        body = (message.get("text") or message.get("caption") or "").strip()
        if chat_id is None:
            return

        # 1. Reply-to-bot nativo → forward al brain como respuesta OWNER
        reply_to = message.get("reply_to_message")
        if reply_to:
            reply_to_msg_id = reply_to.get("message_id")
            if reply_to_msg_id is not None:
                row = self.state.find_by_message(int(chat_id), int(reply_to_msg_id))
                if row is not None and body:
                    self._send_jmf_reply(chat_id, row, body)
                    return

        # 2. Comandos
        if body.startswith("/"):
            try:
                self._handle_command(chat_id, body)
            except Exception:  # noqa: BLE001
                log.exception("Command handler failed")
                self.telegram.send_message(chat_id, "Algo falló procesando ese comando.")
            return

        if not body:
            return

        # 3. Texto plano → si hay ticket awaiting_reply (✍️ Responder fue tocado), envíalo ahí
        row = self.state.find_awaiting_reply(int(chat_id))
        if row is not None:
            self._send_jmf_reply(chat_id, row, body)
            return

        # 4. Sin contexto → asumir instrucción agéntica de OWNER (Phase 1b).
        # Cualquier texto libre que no sea comando ni reply se envía a brain /owner/instruct.
        self._forward_owner_instruction(chat_id, body)

    def _forward_owner_instruction(self, chat_id: int, body: str) -> None:
        """Forwarda instrucción libre de OWNER al brain /owner/instruct."""
        try:
            url = f"{self.settings.brain_url.rstrip('/')}/owner/instruct"
            r = self.brain.http.post(url, json={"text": body, "source": "telegram"}, timeout=60.0)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            log.exception("forward_owner_instruction failed")
            self.telegram.send_message(chat_id, f"❌ No pude procesar la instrucción: {e}")
            return
        reply = data.get("reply") or "(sin respuesta del brain)"
        self.telegram.send_message(chat_id, reply)

    def _send_jmf_reply(self, chat_id: int, row: Any, body: str) -> None:
        """Envía la respuesta de OWNER al brain y actualiza el mensaje del bot."""
        try:
            self.brain.jmf_reply(row.ticket_id, body)
            self.state.set_status(row.ticket_id, "closed")
            self.telegram.edit_message_text(
                chat_id, row.message_id, render_reply_sent(row.ticket_id, body)
            )
            self.telegram.send_message(chat_id, f"✅ Respuesta enviada al paciente (ticket #{row.ticket_id}).")
        except Exception as e:  # noqa: BLE001
            log.exception("Failed forwarding OWNER reply for ticket %s", row.ticket_id)
            self.telegram.send_message(chat_id, f"❌ Falló enviar la respuesta del ticket #{row.ticket_id}: {e}")

    def _handle_command(self, chat_id: int, body: str) -> None:
        token = body.split()[0].lower() if body else ""
        if token in {"/start", "/help", "/menu"}:
            self.telegram.send_message(chat_id, self._render_help())
            return
        if token in {"/pendientes", "/pending"}:
            self._send_pending_tickets(chat_id)
            return
        if token in {"/tickets", "/abiertos"}:
            self._send_all_open_tickets(chat_id)
            return
        if token in {"/stats", "/metricas", "/métricas"}:
            self._send_stats(chat_id)
            return
        # Mensaje libre → recordatorio del menú
        self.telegram.send_message(
            chat_id,
            "No entendí ese comando. Manda /help para ver opciones, "
            "o responde a un ticket existente con la opción ✍️ Responder.",
        )

    def _render_help(self) -> str:
        ui_url = self.settings.ui_url.rstrip("/")
        return (
            "<b>🌸 Iris — bot puente</b>\n\n"
            "Soy el puente entre los pacientes/prospectos de WhatsApp y tú. "
            "Cuando llega un ticket nuevo te aviso aquí con botones.\n\n"
            "<b>Comandos:</b>\n"
            "• /pendientes — tickets esperando tu respuesta\n"
            "• /tickets — todos los tickets activos\n"
            "• /stats — métricas del día (mensajes, costo, crisis)\n"
            "• /help — este mensaje\n\n"
            "<b>Cómo responder un ticket:</b>\n"
            "Cuando te llega un ticket usa los botones inline (✅ Aprobar / ✍️ Responder / 🚫 Cerrar / 📋 Ver thread). "
            "Si tocas ✍️ Responder, simplemente <i>responde</i> al mensaje del bot (long-press → Reply) con tu texto.\n\n"
            f"<b>Panel admin completo:</b> {ui_url}/admin"
        )

    def _send_pending_tickets(self, chat_id: int) -> None:
        try:
            data = self.brain.admin_tickets_live()
        except Exception as e:  # noqa: BLE001
            self.telegram.send_message(chat_id, f"No pude obtener tickets: {e}")
            return
        groups = data.get("groups") or {}
        # Mostrar TODOS los activos (no solo awaiting_jmf): open + awaiting_jmf + awaiting_patient.
        active_statuses = [("awaiting_jmf", "⏳ Esperan tu respuesta"), ("open", "🆕 Recién creados"), ("awaiting_patient", "📤 Esperan al paciente")]
        active_total = sum(len(groups.get(s, [])) for s, _ in active_statuses)
        if active_total == 0:
            self.telegram.send_message(chat_id, "✅ No hay tickets activos en este momento.")
            return

        self.telegram.send_message(
            chat_id,
            f"<b>📥 {active_total} ticket(s) activo(s)</b>\n"
            f"Te mando uno por uno. Usa los botones para responder, cerrar o ver el thread.",
        )

        sent_count = 0
        for status_key, status_label in active_statuses:
            items = groups.get(status_key, [])
            if not items:
                continue
            self.telegram.send_message(chat_id, f"<b>{status_label} ({len(items)})</b>")
            for t in items:
                if sent_count >= 10:
                    self.telegram.send_message(
                        chat_id,
                        f"…y más. Ver el panel completo: {self.settings.ui_url}/admin/tickets",
                    )
                    return
                payload = {
                    "ticket_id": t.get("id"),
                    "thread_id": t.get("thread_id"),
                    "kind": t.get("kind"),
                    "summary": t.get("summary"),
                    "draft": t.get("draft_for_jmf"),
                    "contact_name": t.get("contact_name"),
                    "contact_phone": t.get("contact_phone"),
                    "urgent": t.get("kind") == "urgencia",
                }
                text = render_ticket_message(payload)
                # Para awaiting_patient: indicar que el paciente ya recibió respuesta.
                if status_key == "awaiting_patient" and t.get("jmf_response"):
                    text += f"\n\n<i>Tu última respuesta:</i>\n<blockquote>{_esc(t['jmf_response'][:300])}</blockquote>"
                markup = build_inline_keyboard(payload)
                sent = self.telegram.send_message(chat_id, text, reply_markup=markup)
                try:
                    msg_id = sent.get("result", {}).get("message_id") or sent.get("message_id")
                    if msg_id is not None:
                        self.state.upsert_ticket(
                            ticket_id=int(t["id"]),
                            chat_id=int(chat_id),
                            message_id=int(msg_id),
                            thread_id=t.get("thread_id"),
                            kind=t.get("kind"),
                            status=status_key,
                        )
                        if payload["draft"]:
                            self._draft_cache[int(t["id"])] = payload["draft"]
                except Exception:  # noqa: BLE001
                    log.exception("Failed to upsert state for ticket %s", t.get("id"))
                sent_count += 1

    def _send_all_open_tickets(self, chat_id: int) -> None:
        try:
            data = self.brain.admin_tickets_live()
        except Exception as e:  # noqa: BLE001
            self.telegram.send_message(chat_id, f"No pude obtener tickets: {e}")
            return
        counts = data.get("counts") or {}
        groups = data.get("groups") or {}
        ui = self.settings.ui_url.rstrip("/")
        msg = (
            f"<b>🎫 Tickets activos</b>\n\n"
            f"• Open: {counts.get('open', 0)}\n"
            f"• Esperando tu respuesta: {counts.get('awaiting_jmf', 0)}\n"
            f"• Esperando paciente: {counts.get('awaiting_patient', 0)}\n"
            f"• Cerrados hoy: {counts.get('closed', 0)}\n\n"
            f"<b>Detalle de awaiting_jmf:</b>\n"
        )
        pending = groups.get("awaiting_jmf", [])
        if not pending:
            msg += "  (ninguno)"
        else:
            msg += "\n".join(self._render_ticket_line(t) for t in pending[:5])
            if len(pending) > 5:
                msg += f"\n  …y {len(pending) - 5} más."
        msg += f"\n\nPanel completo: {ui}/admin/tickets"
        self.telegram.send_message(chat_id, msg)

    def _send_stats(self, chat_id: int) -> None:
        try:
            m = self.brain.admin_metrics_today()
        except Exception as e:  # noqa: BLE001
            self.telegram.send_message(chat_id, f"No pude obtener métricas: {e}")
            return
        crisis = m.get("crisis_detections_today", 0)
        crisis_emoji = "🚨" if crisis > 0 else "✅"
        tokens_in = (m.get("tokens_input_haiku", 0) or 0) + (m.get("tokens_input_sonnet", 0) or 0)
        tokens_out = (m.get("tokens_output_haiku", 0) or 0) + (m.get("tokens_output_sonnet", 0) or 0)
        msg = (
            f"<b>📊 Métricas de hoy</b>\n\n"
            f"💬 Mensajes recibidos: {m.get('messages_in_today', 0)}\n"
            f"📤 Respuestas enviadas: {m.get('messages_out_today', 0)}\n"
            f"🎫 Tickets abiertos: {m.get('tickets_open', 0)}\n"
            f"✔️ Cerrados hoy: {m.get('tickets_closed_today', 0)}\n"
            f"{crisis_emoji} Crisis detectadas: {crisis}\n\n"
            f"🔤 Tokens in: {tokens_in:,} · out: {tokens_out:,}\n"
            f"💰 Costo USD hoy: ${m.get('estimated_cost_usd_today', 0):.4f}"
        )
        self.telegram.send_message(chat_id, msg)

    def _render_ticket_line(self, t: dict[str, Any]) -> str:
        emoji = {
            "urgencia": "🚨",
            "posible_urgencia": "⚠️",
            "consulta_cita": "📅",
            "info_curso": "🎓",
            "info_asesoria": "💼",
            "seguimiento_paciente": "🩺",
            "pago_facturacion": "💸",
        }.get(t.get("kind", ""), "📩")
        name = t.get("contact_name") or t.get("contact_phone") or "(sin nombre)"
        summary = (t.get("summary") or "").replace("\n", " ")
        if len(summary) > 80:
            summary = summary[:77] + "…"
        return f"  {emoji} <b>#{t.get('id')}</b> {name} — {summary}"
