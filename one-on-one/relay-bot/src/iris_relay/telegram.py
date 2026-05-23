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
    build_iris_panel_keyboard,
    build_plan_keyboard,
    build_plan_schedule_submenu,
    build_user_question_keyboard,
    render_approved,
    render_closed,
    render_iris_panel,
    render_plan_message,
    render_reply_prompt,
    render_reply_sent,
    render_thread_messages,
    render_ticket_message,
    render_urgent_banner,
    render_user_question,
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

    # --- Feature 1/2/3 endpoints (brain) -------------------------------

    def owner_forward_answer(self, contact_phone: str, answer_text: str) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/owner/forward-answer"
        r = self.http.post(
            url,
            json={"contact_phone": contact_phone, "answer_text": answer_text},
            timeout=self.timeout,
        )
        r.raise_for_status()
        return r.json() if r.content else {}

    def silence_contact(self, contact_phone: str, on: bool = True) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/contacts/{contact_phone}/silence"
        r = self.http.post(url, json={"on": on}, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def close_conversation(self, contact_phone: str) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/contacts/{contact_phone}/close-conversation"
        r = self.http.post(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def iris_pause(self, duration: str) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/iris/pause"
        r = self.http.post(url, json={"duration": duration}, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def iris_status(self) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/iris/status"
        r = self.http.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def iris_silent_toggle(self) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/iris/silent-mode-toggle"
        r = self.http.post(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def iris_active_conversations(self) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/iris/active-conversations"
        r = self.http.get(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def task_execute(self, task_id: int, force: bool = False) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/tasks/{task_id}/execute"
        r = self.http.post(url, params={"force": str(force).lower()}, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def task_cancel(self, task_id: int) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/tasks/{task_id}/cancel"
        r = self.http.post(url, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}

    def task_patch(self, task_id: int, **fields: Any) -> dict[str, Any]:
        url = f"{self.settings.brain_url.rstrip('/')}/tasks/{task_id}/patch"
        r = self.http.post(url, json=fields, timeout=self.timeout)
        r.raise_for_status()
        return r.json() if r.content else {}


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

    def send_report_to_owner(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Feature 1: pregunta de usuario → mensaje a Owner con botones rápidos."""
        chat_id = self.settings.telegram_chat_id
        text = payload.get("text") or ""
        contact_phone = payload.get("contact_phone")
        task_id = payload.get("task_id")
        if contact_phone:
            body = render_user_question(text, contact_phone, task_id=task_id)
            markup = build_user_question_keyboard(contact_phone)
            sent = self.telegram.send_message(chat_id, body, reply_markup=markup)
        else:
            prefix = f"🤖 <b>Iris</b> · task #{task_id}\n" if task_id else "🤖 <b>Iris</b>\n"
            sent = self.telegram.send_message(chat_id, prefix + text)
        return {"telegram_message_id": sent.get("message_id")}

    def send_plan_to_owner(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Feature 2: plan agéntico → preview con botones inline."""
        chat_id = self.settings.telegram_chat_id
        task_id = int(payload["task_id"])
        summary = payload.get("summary") or ""
        plan_text = payload.get("plan_text") or ""
        body = render_plan_message(task_id, summary, plan_text)
        markup = build_plan_keyboard(task_id)
        sent = self.telegram.send_message(chat_id, body, reply_markup=markup)
        return {"telegram_message_id": sent.get("message_id"), "task_id": task_id}

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

            elif action == "usr" and len(parts) >= 3:
                self._handle_callback_usr(cb_id, chat_id, message_id, parts)

            elif action == "plan" and len(parts) >= 3:
                self._handle_callback_plan(cb_id, chat_id, message_id, parts)

            elif action == "iris" and len(parts) >= 2:
                self._handle_callback_iris(cb_id, chat_id, message_id, parts)

            else:
                self.telegram.answer_callback_query(cb_id, "Acción desconocida")
        except Exception as e:  # noqa: BLE001
            log.exception("Callback handling failed")
            try:
                self.telegram.answer_callback_query(cb_id, f"Error: {e}")
            except Exception:  # noqa: BLE001
                pass

    # --- Feature 1: callbacks usr:* (pregunta de usuario) --------------

    _USR_LATER_MSG = (
        "Por el momento el doctor está atendiendo otros pendientes — te confirmo en "
        "unas horas, gracias por tu paciencia 🙏"
    )
    _USR_NO_INFO_MSG = (
        "Por el momento no tengo esa información, déjame consulto y te aviso si encuentro algo."
    )

    def _handle_callback_usr(self, cb_id: str, chat_id: int, message_id: int, parts: list[str]) -> None:
        sub = parts[1]
        phone = ":".join(parts[2:])  # phones no llevan ':' pero por si acaso
        if sub == "reply":
            self.state.set_pending_action(int(chat_id), "usr_reply", {"contact_phone": phone})
            self.telegram.edit_message_text(
                chat_id, message_id,
                f"✍️ Escribe la respuesta para <code>{_esc(phone)}</code> en este chat. "
                "Iris la reenviará al usuario."
            )
            self.telegram.answer_callback_query(cb_id, "Esperando tu mensaje…")
            return

        if sub == "later":
            r = self.brain.owner_forward_answer(phone, self._USR_LATER_MSG)
            if r.get("ok"):
                self.telegram.edit_message_text(chat_id, message_id, "✅ Le dije que confirmas más tarde.")
                self.telegram.answer_callback_query(cb_id, "Enviado")
            else:
                self.telegram.answer_callback_query(cb_id, f"Error: {r.get('error')}")
            return

        if sub == "no_info":
            r = self.brain.owner_forward_answer(phone, self._USR_NO_INFO_MSG)
            if r.get("ok"):
                self.telegram.edit_message_text(chat_id, message_id, "✅ Le dije que no tienes info.")
                self.telegram.answer_callback_query(cb_id, "Enviado")
            else:
                self.telegram.answer_callback_query(cb_id, f"Error: {r.get('error')}")
            return

        if sub == "silence":
            r = self.brain.silence_contact(phone, on=True)
            if r.get("ok"):
                self.telegram.edit_message_text(chat_id, message_id, "🔇 Iris no responderá a este contacto.")
                self.telegram.answer_callback_query(cb_id, "Silenciado")
            else:
                self.telegram.answer_callback_query(cb_id, f"Error: {r.get('error')}")
            return

        if sub == "close":
            r = self.brain.close_conversation(phone)
            if r.get("ok"):
                self.telegram.edit_message_text(chat_id, message_id, "✅ Conversación cerrada.")
                self.telegram.answer_callback_query(cb_id, "Cerrada")
            else:
                self.telegram.answer_callback_query(cb_id, f"Error: {r.get('error')}")
            return

        self.telegram.answer_callback_query(cb_id, "Acción usr desconocida")

    # --- Feature 2: callbacks plan:* (preview de plan agéntico) --------

    def _handle_callback_plan(self, cb_id: str, chat_id: int, message_id: int, parts: list[str]) -> None:
        sub = parts[1]
        try:
            task_id = int(parts[2])
        except ValueError:
            self.telegram.answer_callback_query(cb_id, "task_id inválido")
            return

        if sub == "send":
            r = self.brain.task_execute(task_id, force=True)
            sent = r.get("sent", 0)
            failed = r.get("failed", 0)
            self.telegram.edit_message_text(
                chat_id, message_id,
                f"✅ <b>Plan ejecutado · task #{task_id}</b>\nEnviados: {sent} · Fallidos: {failed}",
            )
            self.telegram.answer_callback_query(cb_id, "Enviado")
            return

        if sub == "schedule":
            self.telegram.edit_message_text(
                chat_id, message_id,
                f"📅 ¿Cuándo enviar el plan task #{task_id}?",
                reply_markup=build_plan_schedule_submenu(task_id),
            )
            self.telegram.answer_callback_query(cb_id)
            return

        if sub == "sched_pick" and len(parts) >= 4:
            choice = parts[3]
            if choice == "custom":
                self.state.set_pending_action(int(chat_id), "plan_sched_custom", {"task_id": task_id})
                self.telegram.edit_message_text(
                    chat_id, message_id,
                    f"⏰ Escribe la fecha/hora (formato <code>YYYY-MM-DD HH:MM</code>, hora local) para task #{task_id}.",
                )
                self.telegram.answer_callback_query(cb_id, "Esperando fecha…")
                return
            sched_iso = self._resolve_schedule_choice(choice)
            if not sched_iso:
                self.telegram.answer_callback_query(cb_id, "Opción desconocida")
                return
            self.brain.task_patch(task_id, scheduled_at=sched_iso)
            self.telegram.edit_message_text(
                chat_id, message_id,
                f"📅 Programada para <code>{_esc(sched_iso)}</code> — el worker la disparará automáticamente.",
            )
            self.telegram.answer_callback_query(cb_id, "Programada")
            return

        if sub == "back":
            # Re-mostrar keyboard de plan (sin re-render del texto original)
            self.telegram.edit_message_text(
                chat_id, message_id,
                f"🎯 Plan task #{task_id} — elige acción:",
                reply_markup=build_plan_keyboard(task_id),
            )
            self.telegram.answer_callback_query(cb_id)
            return

        if sub == "edit":
            self.state.set_pending_action(int(chat_id), "plan_edit", {"task_id": task_id})
            self.telegram.edit_message_text(
                chat_id, message_id,
                f"✍️ Escribe el nuevo texto para task #{task_id}. Iris lo aplicará y te muestra el plan revisado.",
            )
            self.telegram.answer_callback_query(cb_id, "Esperando texto…")
            return

        if sub == "cancel":
            self.brain.task_cancel(task_id)
            self.telegram.edit_message_text(chat_id, message_id, f"❌ Plan task #{task_id} cancelado.")
            self.telegram.answer_callback_query(cb_id, "Cancelado")
            return

        self.telegram.answer_callback_query(cb_id, "Acción plan desconocida")

    def _resolve_schedule_choice(self, choice: str) -> str | None:
        """Mapea tomorrow_9/mon_9/tue_9/fri_9 → ISO 8601 UTC."""
        from datetime import datetime, time, timedelta, timezone
        now = datetime.now(timezone.utc)
        # Suponemos hora local CDMX (UTC-6) — 9am local = 15:00 UTC.
        target_hour_utc = 15
        if choice == "tomorrow_9":
            d = (now + timedelta(days=1)).date()
        elif choice in {"mon_9", "tue_9", "fri_9"}:
            wd_map = {"mon_9": 0, "tue_9": 1, "fri_9": 4}
            target_wd = wd_map[choice]
            days_ahead = (target_wd - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7  # próxima semana
            d = (now + timedelta(days=days_ahead)).date()
        else:
            return None
        dt = datetime.combine(d, time(hour=target_hour_utc, minute=0, tzinfo=timezone.utc))
        return dt.isoformat()

    # --- Feature 3: callbacks iris:* (control panel) -------------------

    def _handle_callback_iris(self, cb_id: str, chat_id: int, message_id: int, parts: list[str]) -> None:
        sub = parts[1]
        if sub == "pause" and len(parts) >= 3:
            duration = parts[2]
            r = self.brain.iris_pause(duration)
            pu = r.get("paused_until")
            self.telegram.edit_message_text(
                chat_id, message_id,
                f"🔇 Iris pausada hasta <code>{_esc(pu)}</code>." if pu else "🔇 Iris pausada.",
            )
            self.telegram.answer_callback_query(cb_id, "Pausada")
            return

        if sub == "resume":
            self.brain.iris_pause("off")
            self.telegram.edit_message_text(chat_id, message_id, "▶️ Iris reactivada.")
            self.telegram.answer_callback_query(cb_id, "Activa")
            return

        if sub == "list_active":
            data = self.brain.iris_active_conversations()
            tasks = data.get("tasks", [])
            if not tasks:
                body = "📊 <b>Conversaciones activas</b>\n\n(ninguna)"
            else:
                lines = ["📊 <b>Conversaciones activas</b>", ""]
                for t in tasks:
                    contacts = ", ".join(t.get("contacts") or []) or "(sin contactos)"
                    lines.append(
                        f"• <b>#{t['task_id']}</b> [{_esc(t.get('status', '?'))}] {_esc(t.get('summary', ''))[:80]}\n  → {_esc(contacts)}"
                    )
                body = "\n".join(lines)
            self.telegram.edit_message_text(chat_id, message_id, body)
            self.telegram.answer_callback_query(cb_id)
            return

        if sub == "silence_contact":
            self.state.set_pending_action(int(chat_id), "iris_silence", {})
            self.telegram.edit_message_text(
                chat_id, message_id,
                "✍️ Pásame el teléfono (10-15 dígitos) del contacto a silenciar.",
            )
            self.telegram.answer_callback_query(cb_id, "Esperando…")
            return

        if sub == "silent_mode_toggle":
            r = self.brain.iris_silent_toggle()
            new = r.get("silent_mode_global")
            self.telegram.edit_message_text(
                chat_id, message_id,
                f"⚙️ Modo silencioso global: <b>{'ON' if new else 'OFF'}</b>.\n"
                f"{'Iris solo reportará al owner, no contestará a usuarios.' if new else 'Iris responde a usuarios normalmente.'}",
            )
            self.telegram.answer_callback_query(cb_id)
            return

        self.telegram.answer_callback_query(cb_id, "Acción iris desconocida")

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        body = (message.get("text") or message.get("caption") or "").strip()
        if chat_id is None:
            return

        # 0. Phase 1c — Ingesta de media del owner por Telegram.
        # Cualquier foto/documento del owner se ingiere automáticamente; el caption
        # define el label (regex "guarda como X" prevalece; si no, caption completo
        # sin hashtags; si no hay caption, label automático con fecha).
        if self._is_owner_chat(chat_id) and (message.get("photo") or message.get("document")):
            try:
                self._handle_owner_media_ingest(chat_id, message, body)
            except Exception:  # noqa: BLE001
                log.exception("owner media ingest failed")
                self.telegram.send_message(chat_id, "❌ No pude guardar la imagen (revisa logs del brain).")
            return

        # 1. Reply-to-bot nativo → forward al brain como respuesta Owner
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

        # 2.5. Pending actions de menús inline_keyboard (usr_reply / plan_edit / plan_sched_custom / iris_silence)
        pending = self.state.get_pending_action(int(chat_id))
        if pending is not None:
            action, payload = pending
            if self._dispatch_pending_action(chat_id, action, payload, body):
                self.state.clear_pending_action(int(chat_id))
                return

        # 3. Texto plano → si hay ticket awaiting_reply (✍️ Responder fue tocado), envíalo ahí
        row = self.state.find_awaiting_reply(int(chat_id))
        if row is not None:
            self._send_jmf_reply(chat_id, row, body)
            return

        # 4. Sin contexto → asumir instrucción agéntica de Owner (Phase 1b).
        # Cualquier texto libre que no sea comando ni reply se envía a brain /owner/instruct.
        self._forward_owner_instruction(chat_id, body)

    # --- Phase 1c — owner media ingest ---------------------------------

    _INGEST_CAPTION_RE = __import__("re").compile(
        r"^\s*(?:guarda|guardar|save)\s+(?:como|as)\s+(?P<label>.+?)\s*$",
        flags=__import__("re").IGNORECASE,
    )
    _TAG_RE = __import__("re").compile(r"#(\w+)")

    def _is_owner_chat(self, chat_id: int) -> bool:
        owner_id = self.settings.telegram_chat_id
        try:
            return str(chat_id) == str(owner_id)
        except Exception:  # noqa: BLE001
            return False

    def _looks_like_ingest_caption(self, text: str) -> bool:
        if not text:
            return False
        stripped = self._TAG_RE.sub("", text).strip()
        return bool(self._INGEST_CAPTION_RE.match(stripped))

    def _parse_ingest_caption(self, text: str) -> tuple[str, list[str]]:
        """Devuelve (label, tags). Reglas:
        - Si caption matchea 'guarda como X' → label = X.
        - Si caption tiene texto pero no matchea → label = caption sin hashtags.
        - Si no hay caption útil → label autogenerado con fecha/hora.
        """
        from datetime import datetime as _dt

        tags = self._TAG_RE.findall(text) if text else []
        stripped = self._TAG_RE.sub("", text or "").strip()
        if stripped:
            m = self._INGEST_CAPTION_RE.match(stripped)
            if m:
                return m.group("label").strip(), tags
            return stripped, tags
        return f"Foto Telegram {_dt.now():%Y-%m-%d %H:%M}", tags

    def _telegram_get_file(self, file_id: str) -> dict[str, Any]:
        url = f"{self.settings.telegram_api_base}/getFile"
        r = self.telegram.http.post(url, json={"file_id": file_id}, timeout=self.settings.http_timeout)
        r.raise_for_status()
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"getFile failed: {data}")
        return data["result"]

    def _telegram_download(self, file_path: str) -> bytes:
        token = self.settings.telegram_bot_token
        url = f"https://api.telegram.org/file/bot{token}/{file_path}"
        r = self.telegram.http.get(url, timeout=self.settings.http_timeout * 2)
        r.raise_for_status()
        return r.content

    def _handle_owner_media_ingest(self, chat_id: int, message: dict[str, Any], caption: str) -> None:
        # Tomar la mejor resolución para fotos, o el documento (si es imagen).
        file_id: str | None = None
        mime: str | None = None
        filename: str | None = None
        if message.get("photo"):
            photos = message["photo"]
            best = photos[-1]
            file_id = best.get("file_id")
            mime = "image/jpeg"  # Telegram convierte fotos a jpeg
            filename = f"telegram-{message.get('message_id', 'photo')}.jpg"
        elif message.get("document"):
            doc = message["document"]
            mime = (doc.get("mime_type") or "").lower()
            if mime not in {"image/jpeg", "image/png", "image/webp", "application/pdf"}:
                self.telegram.send_message(chat_id, f"❌ Tipo {mime} no soportado (solo jpg/png/webp/pdf).")
                return
            file_id = doc.get("file_id")
            filename = doc.get("file_name") or f"telegram-{message.get('message_id', 'doc')}"
        if not file_id:
            return

        # 1. getFile → file_path
        info = self._telegram_get_file(file_id)
        file_path = info.get("file_path")
        if not file_path:
            self.telegram.send_message(chat_id, "❌ Telegram no devolvió file_path.")
            return
        # 2. Download blob
        blob = self._telegram_download(file_path)
        # 3. Parse label + tags (label SIEMPRE viene non-empty con las nuevas reglas)
        label, tags = self._parse_ingest_caption(caption)
        # 4. POST a brain /media/upload
        import json as _json
        files = {"file": (filename, blob, mime)}
        data: dict[str, str] = {"source": "telegram", "label": label}
        if tags:
            data["tags"] = _json.dumps(tags)
        url = f"{self.settings.brain_url.rstrip('/')}/media/upload"
        try:
            r = self.telegram.http.post(url, files=files, data=data, timeout=30.0)
            r.raise_for_status()
            res = r.json()
        except Exception as e:  # noqa: BLE001
            log.exception("brain /media/upload failed")
            self.telegram.send_message(chat_id, f"❌ Error subiendo al brain: {e}")
            return
        dedupe = ", dedupe" if res.get("dedupe") else ""
        self.telegram.send_message(
            chat_id,
            f"📸 Guardada como '<b>{label}</b>' "
            f"(id={res['id']}, source=telegram{dedupe}) ✓",
        )

    def _forward_owner_instruction(self, chat_id: int, body: str) -> None:
        """Forwarda instrucción libre de Owner al brain /owner/instruct."""
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
        """Envía la respuesta de Owner al brain y actualiza el mensaje del bot."""
        try:
            self.brain.jmf_reply(row.ticket_id, body)
            self.state.set_status(row.ticket_id, "closed")
            self.telegram.edit_message_text(
                chat_id, row.message_id, render_reply_sent(row.ticket_id, body)
            )
            self.telegram.send_message(chat_id, f"✅ Respuesta enviada al usuario (ticket #{row.ticket_id}).")
        except Exception as e:  # noqa: BLE001
            log.exception("Failed forwarding Owner reply for ticket %s", row.ticket_id)
            self.telegram.send_message(chat_id, f"❌ Falló enviar la respuesta del ticket #{row.ticket_id}: {e}")

    def _dispatch_pending_action(
        self, chat_id: int, action: str, payload: dict, body: str
    ) -> bool:
        """Devuelve True si consumió el body (pending action ejecutado)."""
        if action == "usr_reply":
            phone = payload.get("contact_phone")
            if not phone:
                return False
            try:
                r = self.brain.owner_forward_answer(phone, body)
            except Exception as e:  # noqa: BLE001
                log.exception("usr_reply forward failed")
                self.telegram.send_message(chat_id, f"❌ No pude reenviar: {e}")
                return True
            if r.get("ok"):
                self.telegram.send_message(
                    chat_id,
                    f"✅ Respuesta enviada a <code>{_esc(phone)}</code>.",
                )
            else:
                self.telegram.send_message(chat_id, f"❌ Error: {r.get('error')}")
            return True

        if action == "iris_silence":
            phone = body.strip()
            if not phone:
                return False
            try:
                r = self.brain.silence_contact(phone, on=True)
            except Exception as e:  # noqa: BLE001
                self.telegram.send_message(chat_id, f"❌ Error: {e}")
                return True
            if r.get("ok"):
                self.telegram.send_message(
                    chat_id,
                    f"🔇 Contacto silenciado: <code>{_esc(phone)}</code>"
                    + (f" ({_esc(r.get('name') or '')})" if r.get("name") else ""),
                )
            else:
                self.telegram.send_message(chat_id, f"❌ {r.get('error')}")
            return True

        if action == "plan_sched_custom":
            task_id = payload.get("task_id")
            # Parse "YYYY-MM-DD HH:MM" como hora local CDMX (UTC-6).
            from datetime import datetime, timezone, timedelta
            try:
                dt_local = datetime.strptime(body.strip(), "%Y-%m-%d %H:%M")
            except ValueError:
                self.telegram.send_message(
                    chat_id,
                    "❌ Formato inválido. Usa <code>YYYY-MM-DD HH:MM</code>.",
                )
                return False  # mantener pending
            # Convertir CDMX (UTC-6) → UTC
            dt_utc = dt_local.replace(tzinfo=timezone(timedelta(hours=-6))).astimezone(timezone.utc)
            try:
                self.brain.task_patch(int(task_id), scheduled_at=dt_utc.isoformat())
            except Exception as e:  # noqa: BLE001
                self.telegram.send_message(chat_id, f"❌ Error: {e}")
                return True
            self.telegram.send_message(
                chat_id,
                f"📅 Task #{task_id} programada para <code>{_esc(dt_utc.isoformat())}</code>.",
            )
            return True

        if action == "plan_edit":
            task_id = payload.get("task_id")
            # Guardamos el nuevo message_template en context. La task se quedará pending
            # hasta que el owner aprete "✅ Enviar ahora" o programe.
            try:
                self.brain.task_patch(int(task_id), message_template=body)
            except Exception as e:  # noqa: BLE001
                self.telegram.send_message(chat_id, f"❌ Error: {e}")
                return True
            self.telegram.send_message(
                chat_id,
                f"✍️ Task #{task_id} actualizada con tu nuevo texto. Pídele a Iris el plan revisado o usa /tasks.",
            )
            return True

        return False

    def _handle_command(self, chat_id: int, body: str) -> None:
        token = body.split()[0].lower() if body else ""
        if token in {"/start", "/help", "/menu"}:
            self.telegram.send_message(chat_id, self._render_help())
            return
        if token == "/iris":
            try:
                status = self.brain.iris_status()
            except Exception as e:  # noqa: BLE001
                status = {"error": str(e)}
            self.telegram.send_message(
                chat_id,
                render_iris_panel(status),
                reply_markup=build_iris_panel_keyboard(),
            )
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
            "Soy el puente entre los usuarios/prospectos de WhatsApp y tú. "
            "Cuando llega un ticket nuevo te aviso aquí con botones.\n\n"
            "<b>Comandos:</b>\n"
            "• /iris — panel de control (pausa, silencio, conversaciones activas)\n"
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
        active_statuses = [("awaiting_jmf", "⏳ Esperan tu respuesta"), ("open", "🆕 Recién creados"), ("awaiting_patient", "📤 Esperan al usuario")]
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
                # Para awaiting_patient: indicar que el usuario ya recibió respuesta.
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
            f"• Esperando usuario: {counts.get('awaiting_patient', 0)}\n"
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
