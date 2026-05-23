"""Iris agéntica — lógica de outbound, task tracking, owner reporting.

Funciones de soporte para los tools agentic_*. La capa de tools en tools.py
las llama y devuelve resultados a Anthropic SDK.

Decisiones congeladas:
- Confirmación explícita: Iris construye plan, Owner aprueba, ENTONCES se envía.
- Canal de instrucción: Telegram (POST a relay-bot endpoint).
- Reportes en vivo: ping a Telegram cada vez que un target responde.
- Send is final: WhatsApp no permite unsend reliable post-segundos.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import or_, select

from .config import settings
from .db import get_session
from .models import Contact, Task, TaskTarget, Thread, Message, MessageDirection

log = logging.getLogger("iris_brain.agentic")


# --- Anti-saturación: rate-limit por contacto -------------------------------
#
# Reglas (decisión congelada 2026-05-22):
# - Máximo 2 outbound por contacto en 24h. Al 3ro Iris debe pedir aprobación.
# - silent_mode=True → NO se envía nada a ese contacto.
# - paused_until > now() → NO se envía hasta esa fecha.
# - El counter rolling se resetea cuando outbound_count_reset_at < now() - 24h.

OUTBOUND_RATE_LIMIT_24H = 2


def _enforce_outbound_rate_limit(contact_id: int) -> dict[str, Any] | None:
    """Devuelve dict de error si el envío debe bloquearse; None si está OK.

    Si OK, también incrementa contador y deja last_outbound_at=now.
    Llamar DENTRO de una sesión propia (esta función abre la suya).
    """
    now = datetime.now(timezone.utc)
    with get_session() as s:
        c = s.get(Contact, contact_id)
        if c is None:
            return {"ok": False, "error": "contacto no existe"}
        # Silent mode → bloqueo duro
        if getattr(c, "silent_mode", False):
            return {
                "ok": False,
                "error": "contact_silent_mode",
                "hint": "El owner silenció a este contacto vía /iris. NO insistas.",
            }
        # Pausa explícita
        paused_until = getattr(c, "paused_until", None)
        if paused_until is not None:
            pu = paused_until if paused_until.tzinfo else paused_until.replace(tzinfo=timezone.utc)
            if pu > now:
                return {
                    "ok": False,
                    "error": "contact_paused",
                    "paused_until": pu.isoformat(),
                    "hint": "Contacto pausado por el owner. NO envíes hasta esa fecha.",
                }
        # Reset rolling window si pasaron 24h
        reset_at = getattr(c, "outbound_count_reset_at", None)
        needs_reset = (
            reset_at is None
            or (reset_at if reset_at.tzinfo else reset_at.replace(tzinfo=timezone.utc))
            < now - timedelta(hours=24)
        )
        if needs_reset:
            c.outbound_count_24h = 0
            c.outbound_count_reset_at = now
        # Verificar límite
        if (c.outbound_count_24h or 0) >= OUTBOUND_RATE_LIMIT_24H:
            return {
                "ok": False,
                "error": "rate_limit_24h_excedido_para_este_contacto",
                "hint": (
                    f"Ya enviaste {OUTBOUND_RATE_LIMIT_24H} mensajes a este contacto en las "
                    "últimas 24h. PIDE aprobación explícita al owner antes de mandar otro."
                ),
                "outbound_count_24h": c.outbound_count_24h,
                "reset_at": (c.outbound_count_reset_at or now).isoformat(),
            }
        # OK: incrementar y registrar
        c.outbound_count_24h = (c.outbound_count_24h or 0) + 1
        c.last_outbound_at = now
        return None



# --- Search -----------------------------------------------------------------

def search_contacts(query: str, kind: str | None = None, limit: int = 10) -> dict[str, Any]:
    """Búsqueda fuzzy por name/phone/notes. Útil para resolver 'manda a Roberto'."""
    if not query or not query.strip():
        return {"found": False, "items": [], "count": 0}
    pattern = f"%{query.strip()}%"
    with get_session() as s:
        stmt = select(Contact).where(
            or_(
                Contact.name.ilike(pattern),
                Contact.phone.ilike(pattern),
                Contact.notes.ilike(pattern),
            )
        )
        if kind:
            stmt = stmt.where(Contact.kind == kind)
        stmt = stmt.order_by(Contact.last_seen.desc().nullslast()).limit(max(1, min(limit, 20)))
        rows = list(s.scalars(stmt))
        items = [
            {
                "id": c.id,
                "name": c.name,
                "phone": c.phone,
                "kind": c.kind.value if c.kind else "otro",
                "notes": (c.notes or "")[:200],
            }
            for c in rows
        ]
        return {"found": bool(items), "count": len(items), "items": items}


# --- Task lifecycle ---------------------------------------------------------

def create_task(
    owner_id: int,
    kind: str,
    summary: str,
    raw_instruction: str | None,
    target_contact_ids: list[int],
    context: dict | None = None,
    expected_names: list[str] | None = None,
) -> dict[str, Any]:
    """Crea task + N task_targets en status='pending'.

    No envía mensajes — eso lo hace send_outbound después de la confirmación de Owner.

    Guardrails:
    - owner_id NUNCA puede estar en target_contact_ids (Iris no debe escribirle al owner).
    - target_contact_ids no puede estar vacío.
    - Si expected_names está presente, cada nombre debe matchear el name del contact correspondiente
      (fuzzy: substring case-insensitive, alguna palabra en común). Si no, rechaza.
    """
    if not target_contact_ids:
        return {"ok": False, "error": "sin targets"}
    # Dedupe + guardrail: owner nunca es target
    target_contact_ids = [int(t) for t in target_contact_ids if int(t) != int(owner_id)]
    if not target_contact_ids:
        return {"ok": False, "error": "todos los target_contact_ids eran el owner — Iris no puede escribirse a sí misma ni al doctor"}
    target_contact_ids = list(dict.fromkeys(target_contact_ids))  # dedupe preserve order

    # Validación nombre↔contact_id si expected_names presente.
    # Si hay mismatch, AUTO-CORRIGE buscando el contact por nombre.
    auto_corrections: list[dict] = []
    if expected_names:
        if len(expected_names) != len(target_contact_ids):
            return {
                "ok": False,
                "error": f"len(expected_names)={len(expected_names)} != len(target_contact_ids)={len(target_contact_ids)}. Pasa la misma cantidad en ambos arrays, en mismo orden.",
            }
        corrected_ids: list[int] = []
        with get_session() as s:
            for expected, cid in zip(expected_names, target_contact_ids):
                c = s.get(Contact, cid)
                actual = (c.name or "").lower().strip() if c is not None else ""
                exp = expected.lower().strip()
                actual_words = {w for w in actual.split() if len(w) >= 3}
                exp_words = {w for w in exp.split() if len(w) >= 3}

                if c is not None and (actual_words & exp_words):
                    # Match OK, conserva el cid
                    corrected_ids.append(cid)
                    continue

                # MISMATCH — auto-resolver por nombre
                log.warning(
                    "create_task AUTO-CORRECT: cid=%s name='%s' no matchea expected='%s' — buscando…",
                    cid, c.name if c else "(missing)", expected,
                )
                # Buscar candidatos por nombre (whole expected as query)
                pattern = f"%{expected.strip()}%"
                candidates = list(
                    s.scalars(
                        select(Contact).where(Contact.name.ilike(pattern)).limit(5)
                    )
                )
                if not candidates:
                    # Fallback: buscar por cada palabra del nombre, intersección
                    found = None
                    for word in exp_words:
                        rows = list(
                            s.scalars(select(Contact).where(Contact.name.ilike(f"%{word}%")).limit(20))
                        )
                        if not found:
                            found = set(r.id for r in rows)
                        else:
                            found &= set(r.id for r in rows)
                        if found is not None and len(found) == 1:
                            cand_id = next(iter(found))
                            candidates = [s.get(Contact, cand_id)]
                            break
                if not candidates:
                    return {
                        "ok": False,
                        "error": (
                            f"No encontré ningún contacto que matchee con '{expected}'. "
                            f"Pídele a Owner el teléfono específico o varía el nombre."
                        ),
                    }
                if len(candidates) > 1:
                    options = ", ".join(f"id={x.id}/'{x.name}'" for x in candidates[:5])
                    return {
                        "ok": False,
                        "error": (
                            f"Encontré {len(candidates)} candidatos para '{expected}': {options}. "
                            f"Pídele a Owner que elija o re-busca con criterio más específico (apellido completo, etc)."
                        ),
                    }
                # Único match — usar y registrar la corrección
                correct = candidates[0]
                if correct.id != cid:
                    auto_corrections.append({
                        "expected": expected,
                        "iris_passed_cid": cid,
                        "iris_passed_name": c.name if c else None,
                        "auto_corrected_to_cid": correct.id,
                        "auto_corrected_to_name": correct.name,
                    })
                corrected_ids.append(correct.id)
        target_contact_ids = corrected_ids
    with get_session() as s:
        t = Task(
            owner_id=owner_id,
            kind=kind,
            summary=summary[:500],
            raw_instruction=raw_instruction,
            status="pending",
            context=context,
        )
        s.add(t)
        s.flush()
        targets_info = []
        for cid in target_contact_ids:
            tt = TaskTarget(task_id=t.id, contact_id=cid, status="pending")
            s.add(tt)
            s.flush()  # para obtener tt.id
            # Lookup nombre del contact para devolverlo
            c = s.get(Contact, cid)
            targets_info.append({
                "target_id": tt.id,
                "contact_id": cid,
                "contact_name": c.name if c else None,
                "contact_phone": c.phone if c else None,
            })
        result: dict[str, Any] = {
            "ok": True,
            "task_id": t.id,
            "target_count": len(target_contact_ids),
            "targets": targets_info,  # USA estos target_id en send_outbound (no contact_id)
        }
        if auto_corrections:
            result["auto_corrections"] = auto_corrections
            result["warning"] = (
                "Hubo correcciones automáticas — algunos contact_id que pasaste no coincidían con expected_names. "
                "El server resolvió por nombre. REVISA targets[] antes de continuar y avísale a Owner en tu reporte de plan."
            )
        return result


def list_active_tasks(owner_id: int | None = None, limit: int = 20) -> dict[str, Any]:
    with get_session() as s:
        stmt = select(Task).where(Task.status.notin_(["complete", "cancelled"]))
        if owner_id is not None:
            stmt = stmt.where(Task.owner_id == owner_id)
        stmt = stmt.order_by(Task.updated_at.desc()).limit(limit)
        rows = list(s.scalars(stmt))
        items = []
        for t in rows:
            target_count = s.scalar(
                select(__import__("sqlalchemy").func.count())
                .select_from(TaskTarget)
                .where(TaskTarget.task_id == t.id)
            ) or 0
            responded = s.scalar(
                select(__import__("sqlalchemy").func.count())
                .select_from(TaskTarget)
                .where(TaskTarget.task_id == t.id, TaskTarget.status == "responded")
            ) or 0
            items.append({
                "id": t.id,
                "kind": t.kind,
                "summary": t.summary,
                "status": t.status,
                "targets": target_count,
                "responded": responded,
                "created_at": t.created_at.isoformat() if t.created_at else None,
            })
        return {"items": items, "count": len(items)}


def update_task_status(task_id: int, status: str, note: str | None = None) -> dict[str, Any]:
    valid = {"pending", "in_progress", "awaiting_responses", "complete", "cancelled"}
    if status not in valid:
        return {"ok": False, "error": f"status inválido: {status}"}
    with get_session() as s:
        t = s.get(Task, task_id)
        if t is None:
            return {"ok": False, "error": "task no existe"}
        t.status = status
        if status in ("complete", "cancelled"):
            t.completed_at = datetime.now(timezone.utc)
        return {"ok": True, "task_id": task_id, "status": status}


# --- Outbound send ----------------------------------------------------------

def send_outbound(task_id: int, target_id: int, body: str) -> dict[str, Any]:
    """Envía mensaje al target via wa-listener, persiste en messages, actualiza task_target.

    Retorna {ok, message_id?, error?}.
    """
    if not body or not body.strip():
        return {"ok": False, "error": "body vacío"}
    body = body.strip()

    with get_session() as s:
        tt = s.get(TaskTarget, target_id)
        if tt is None or tt.task_id != task_id:
            return {"ok": False, "error": "task_target no existe o no pertenece a la task"}
        c = s.get(Contact, tt.contact_id)
        if c is None:
            return {"ok": False, "error": "contacto no existe"}
        # Guardrail: nunca enviar al owner (Iris no se escribe al doctor por WA)
        task = s.get(Task, task_id)
        if task is not None and int(tt.contact_id) == int(task.owner_id):
            log.error("send_outbound BLOCKED: target_id=%s es el owner del task %s", target_id, task_id)
            tt.status = "failed"
            return {"ok": False, "error": "target es el owner — no se permite escribirle por WhatsApp"}
        # Anti-saturación: chequeo + incremento (rate-limit / silent / pause)
        contact_id_for_limit = int(tt.contact_id)
    rl = _enforce_outbound_rate_limit(contact_id_for_limit)
    if rl is not None:
        log.warning("send_outbound rate-limited contact_id=%s err=%s", contact_id_for_limit, rl)
        with get_session() as s:
            tt2 = s.get(TaskTarget, target_id)
            if tt2 is not None:
                tt2.status = "failed"
        return rl
    with get_session() as s:
        # Resolver/crear thread para este contacto (igual que sessions.open_or_get_thread)
        c = s.get(Contact, contact_id_for_limit)
        if c is None:
            return {"ok": False, "error": "contacto no existe"}
        thread = s.scalar(
            select(Thread)
            .where(Thread.contact_id == c.id)
            .order_by(Thread.opened_at.desc())
            .limit(1)
        )
        if thread is None:
            thread = Thread(contact_id=c.id, channel="whatsapp")
            s.add(thread)
            s.flush()
        thread_id = thread.id
        phone = c.phone

    # POST a wa-listener
    wa_url = (settings.CONTACT_RELAY_WEBHOOK or "http://localhost:8099/send-to-contact")
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(wa_url, json={
                "type": "outbound",
                "phone": phone,
                "body": body,
                "thread_id": thread_id,
            })
            r.raise_for_status()
            wa_resp = r.json()
    except httpx.HTTPError as e:
        log.exception("send_outbound: wa-listener falló")
        with get_session() as s:
            tt2 = s.get(TaskTarget, target_id)
            if tt2 is not None:
                tt2.status = "failed"
        return {"ok": False, "error": str(e)}

    if not wa_resp.get("ok"):
        log.warning("send_outbound: wa-listener returned %s", wa_resp)
        with get_session() as s:
            tt2 = s.get(TaskTarget, target_id)
            if tt2 is not None:
                tt2.status = "failed"
        return {"ok": False, "error": wa_resp.get("error", "wa_listener_fail")}

    # Persistir en messages y actualizar task_target
    now = datetime.now(timezone.utc)
    with get_session() as s:
        m = Message(
            thread_id=thread_id,
            direction=MessageDirection.out,
            body=body,
            model_used="agentic_outbound",
        )
        s.add(m)
        tt2 = s.get(TaskTarget, target_id)
        if tt2 is not None:
            tt2.message_sent = body
            tt2.message_sent_at = now
            tt2.thread_id = thread_id
            tt2.status = "sent"
        # Si todos los targets están sent → task pasa a awaiting_responses
        from sqlalchemy import func as sa_func
        pending_count = s.scalar(
            select(sa_func.count())
            .select_from(TaskTarget)
            .where(TaskTarget.task_id == task_id, TaskTarget.status == "pending")
        ) or 0
        if pending_count == 0:
            t = s.get(Task, task_id)
            if t is not None and t.status in ("pending", "in_progress"):
                t.status = "awaiting_responses"

    return {
        "ok": True,
        "message_id": wa_resp.get("message_id"),
        "target_id": target_id,
        "thread_id": thread_id,
    }


# --- Media outbound (Phase 1c) ----------------------------------------------


def find_media(query: str, limit: int = 5, source: str | None = None) -> dict[str, Any]:
    """Wrap de media.find_media para uso como tool agéntica."""
    from . import media as media_mod
    return media_mod.find_media(query, limit=limit, source=source)


def import_marketing_asset(url: str, label: str | None = None, tags: list[str] | None = None) -> dict[str, Any]:
    """Descarga URL whitelisted (marketing.*) y persiste como MediaAsset."""
    from . import media as media_mod
    try:
        r = media_mod.ingest_from_url(url, label=label, tags=tags, source="marketing")
        return {"ok": True, **r, "asset_id": r["id"]}
    except media_mod.MediaError as e:
        return {"ok": False, "error": e.code, "detail": str(e)}


def send_outbound_media(
    task_id: int,
    target_id: int,
    asset_id: int,
    caption: str | None = None,
    body_text: str | None = None,
) -> dict[str, Any]:
    """Envía imagen al target via wa-listener, persiste en messages con media_asset_id.

    Si body_text viene, el wa-listener manda primero el texto y luego la imagen.
    Caption puede ser corto y separado del body (ej. "¡Promo ACLS!" como caption +
    body con saludo personalizado y link de inscripción).
    """
    from . import media as media_mod

    with get_session() as s:
        tt = s.get(TaskTarget, target_id)
        if tt is None or tt.task_id != task_id:
            return {"ok": False, "error": "task_target no existe o no pertenece a la task"}
        c = s.get(Contact, tt.contact_id)
        if c is None:
            return {"ok": False, "error": "contacto no existe"}
        task = s.get(Task, task_id)
        if task is not None and int(tt.contact_id) == int(task.owner_id):
            log.error("send_outbound_media BLOCKED: target_id=%s es el owner", target_id)
            tt.status = "failed"
            return {"ok": False, "error": "target es el owner — no se permite enviar media por WhatsApp"}
        media_contact_id = int(tt.contact_id)
        # Anti-saturación se chequea fuera de la sesión actual (abre la suya).
        # Valida que el asset exista (y no esté borrado)
        asset = s.get(__import__("iris_brain.models", fromlist=["MediaAsset"]).MediaAsset, asset_id)
        if asset is None or asset.deleted_at is not None:
            return {"ok": False, "error": "media_asset no existe o fue borrado"}
        # Caption final (puede ir None)
        if caption is not None:
            caption = caption[:1024]  # límite WA
        # Thread
        thread = s.scalar(
            select(Thread)
            .where(Thread.contact_id == c.id)
            .order_by(Thread.opened_at.desc())
            .limit(1)
        )
        if thread is None:
            thread = Thread(contact_id=c.id, channel="whatsapp")
            s.add(thread)
            s.flush()
        thread_id = thread.id
        phone = c.phone
        asset_label = asset.label or asset.filename

    # Anti-saturación: bloquea / incrementa contador. Si bloquea, marca task_target failed.
    rl = _enforce_outbound_rate_limit(media_contact_id)
    if rl is not None:
        log.warning("send_outbound_media rate-limited contact_id=%s err=%s", media_contact_id, rl)
        with get_session() as s:
            tt2 = s.get(TaskTarget, target_id)
            if tt2 is not None:
                tt2.status = "failed"
        return rl

    # POST a wa-listener (payload extendido con media)
    wa_url = (settings.CONTACT_RELAY_WEBHOOK or "http://localhost:8099/send-to-contact")
    media_url = f"{settings.MEDIA_INTERNAL_URL.rstrip('/')}/media/{asset_id}/raw"
    payload: dict[str, Any] = {
        "type": "outbound_media",
        "phone": phone,
        "thread_id": thread_id,
        "media": {
            "type": "image",
            "url": media_url,
            "caption": caption,
        },
    }
    if body_text:
        payload["body"] = body_text[:4096]  # límite de seguridad
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(wa_url, json=payload)
            r.raise_for_status()
            wa_resp = r.json()
    except httpx.HTTPError as e:
        log.exception("send_outbound_media: wa-listener falló")
        with get_session() as s:
            tt2 = s.get(TaskTarget, target_id)
            if tt2 is not None:
                tt2.status = "failed"
        return {"ok": False, "error": str(e)}

    if not wa_resp.get("ok"):
        log.warning("send_outbound_media: wa-listener returned %s", wa_resp)
        with get_session() as s:
            tt2 = s.get(TaskTarget, target_id)
            if tt2 is not None:
                tt2.status = "failed"
        return {"ok": False, "error": wa_resp.get("error", "wa_listener_fail")}

    # Persistir message + actualizar task_target
    now = datetime.now(timezone.utc)
    # Si hubo body_text (texto previo a la imagen), regístralo como mensaje aparte
    if body_text:
        with get_session() as s:
            s.add(Message(
                thread_id=thread_id,
                direction=MessageDirection.out,
                body=body_text,
                model_used="agentic_outbound_media_text",
            ))
    body_for_record = caption or body_text or f"[imagen: {asset_label}]"
    with get_session() as s:
        m = Message(
            thread_id=thread_id,
            direction=MessageDirection.out,
            body=body_for_record,
            model_used="agentic_outbound_media",
            media_asset_id=asset_id,
            media_caption=caption,
        )
        s.add(m)
        tt2 = s.get(TaskTarget, target_id)
        if tt2 is not None:
            tt2.message_sent = (body_text + " | " + (caption or "")) if body_text else body_for_record
            tt2.message_sent_at = now
            tt2.thread_id = thread_id
            tt2.status = "sent"
        from sqlalchemy import func as sa_func
        pending_count = s.scalar(
            select(sa_func.count())
            .select_from(TaskTarget)
            .where(TaskTarget.task_id == task_id, TaskTarget.status == "pending")
        ) or 0
        if pending_count == 0:
            t = s.get(Task, task_id)
            if t is not None and t.status in ("pending", "in_progress"):
                t.status = "awaiting_responses"

    # Incrementa use_count + last_used_at
    media_mod.record_use(asset_id)

    return {
        "ok": True,
        "message_id": wa_resp.get("message_id"),
        "target_id": target_id,
        "thread_id": thread_id,
        "asset_id": asset_id,
    }


# --- Owner reporting --------------------------------------------------------

def report_to_owner(
    text: str,
    task_id: int | None = None,
    contact_phone: str | None = None,
) -> dict[str, Any]:
    """Manda mensaje a Owner en Telegram.

    Si `contact_phone` viene, intenta primero el relay-bot HTTP
    `/report-to-owner` para que el mensaje se mande CON inline_keyboard
    (botones rápidos: Responder / Te confirmo más tarde / No info / Silenciar /
    Cerrar). Si el relay-bot no responde, hace fallback a Telegram directo
    (sin botones).
    """
    if not text or not text.strip():
        return {"ok": False, "error": "text vacío"}

    # Path 1: con contact_phone → relay-bot con keyboard
    if contact_phone:
        relay_url = getattr(settings, "JMF_RELAY_WEBHOOK", None)
        if relay_url:
            # JMF_RELAY_WEBHOOK apunta a /send-to-jmf; reemplazamos el último segmento.
            base = relay_url.rsplit("/", 1)[0]
            url = f"{base}/report-to-owner"
            payload: dict[str, Any] = {
                "text": text,
                "contact_phone": contact_phone,
            }
            if task_id is not None:
                payload["task_id"] = task_id
            try:
                with httpx.Client(timeout=10) as c:
                    r = c.post(url, json=payload)
                    r.raise_for_status()
                return {"ok": True, "via": "relay-bot"}
            except httpx.HTTPError as e:
                log.warning("report_to_owner relay-bot falló (%s) — fallback a Telegram directo", e)

    # Path 2: fallback / texto plano vía Telegram directo
    bot_token = settings.TELEGRAM_BOT_TOKEN if hasattr(settings, "TELEGRAM_BOT_TOKEN") else None
    chat_id = settings.TELEGRAM_CHAT_ID if hasattr(settings, "TELEGRAM_CHAT_ID") else None
    if not bot_token or not chat_id:
        log.warning("report_to_owner: TELEGRAM_BOT_TOKEN/CHAT_ID no configurados")
        return {"ok": False, "error": "telegram_not_configured"}

    prefix = f"🤖 <b>Iris</b> · task #{task_id}\n" if task_id else "🤖 <b>Iris</b>\n"
    try:
        with httpx.Client(timeout=10) as c:
            r = c.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data={"chat_id": chat_id, "text": prefix + text, "parse_mode": "HTML"},
            )
            r.raise_for_status()
        return {"ok": True, "via": "telegram_direct"}
    except httpx.HTTPError as e:
        log.exception("report_to_owner failed")
        return {"ok": False, "error": str(e)}


def report_plan_to_owner(task_id: int, summary: str, plan_text: str) -> dict[str, Any]:
    """Manda preview del plan a Owner con keyboard inline (Send / Schedule / Edit / Cancel).

    Llama al relay-bot. Si no está disponible, fallback a `report_to_owner`
    (sin botones) para que Owner al menos vea el plan.
    """
    relay_url = getattr(settings, "JMF_RELAY_WEBHOOK", None)
    if relay_url:
        base = relay_url.rsplit("/", 1)[0]
        url = f"{base}/report-plan-to-owner"
        try:
            with httpx.Client(timeout=10) as c:
                r = c.post(url, json={"task_id": task_id, "summary": summary, "plan_text": plan_text})
                r.raise_for_status()
            return {"ok": True, "via": "relay-bot"}
        except httpx.HTTPError as e:
            log.warning("report_plan_to_owner relay-bot falló (%s)", e)
    # Fallback
    body = f"<b>Plan task #{task_id}</b>\n{summary}\n\n{plan_text}\n\n(El bot no pudo renderizar botones — usa /tasks o el panel.)"
    return report_to_owner(body, task_id=task_id)


# --- Silenciar / cerrar conversación con contacto ---------------------------

def silence_contact(contact_phone: str, on: bool = True) -> dict[str, Any]:
    """Marca un contacto con silent_mode=on. Si on=True, Iris ya no le contestará."""
    from .sessions import sanitize_phone

    p = sanitize_phone(contact_phone)
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            return {"ok": False, "error": "contacto no existe", "phone": p}
        c.silent_mode = bool(on)
        return {
            "ok": True,
            "phone": p,
            "contact_id": c.id,
            "name": c.name,
            "silent_mode": c.silent_mode,
        }


def pause_contact(contact_phone: str, hours: int) -> dict[str, Any]:
    """Pausa outbound a un contacto por N horas (None para limpiar)."""
    from .sessions import sanitize_phone

    p = sanitize_phone(contact_phone)
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            return {"ok": False, "error": "contacto no existe", "phone": p}
        if hours and hours > 0:
            c.paused_until = datetime.now(timezone.utc) + timedelta(hours=int(hours))
        else:
            c.paused_until = None
        return {
            "ok": True,
            "phone": p,
            "contact_id": c.id,
            "paused_until": c.paused_until.isoformat() if c.paused_until else None,
        }


def close_conversation_with_contact(contact_phone: str) -> dict[str, Any]:
    """Cierra la conversación con un contacto desde el menú del owner.

    Efectos:
    - notes_append con timestamp [closed by owner YYYY-MM-DD].
    - Cancela tasks activas que tengan a este contacto como target pending/sent.
    """
    from .sessions import sanitize_phone

    p = sanitize_phone(contact_phone)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cancelled_tasks: list[int] = []
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            return {"ok": False, "error": "contacto no existe", "phone": p}
        line = f"[{stamp}] closed by owner"
        c.notes = f"{c.notes}\n{line}" if c.notes else line
        # Cancelar task_targets pendientes para este contacto + sus tasks
        from sqlalchemy import update as sa_update
        s.execute(
            sa_update(TaskTarget)
            .where(TaskTarget.contact_id == c.id, TaskTarget.status.in_(["pending", "sent"]))
            .values(status="cancelled")
        )
        task_ids = list(
            s.scalars(
                select(TaskTarget.task_id)
                .where(TaskTarget.contact_id == c.id)
                .distinct()
            )
        )
        for tid in task_ids:
            t = s.get(Task, tid)
            if t is not None and t.status not in ("complete", "cancelled"):
                # ¿Quedan targets activos?
                from sqlalchemy import func as sa_func
                active_left = s.scalar(
                    select(sa_func.count())
                    .select_from(TaskTarget)
                    .where(
                        TaskTarget.task_id == tid,
                        TaskTarget.status.in_(["pending", "sent"]),
                    )
                ) or 0
                if active_left == 0:
                    t.status = "cancelled"
                    t.completed_at = datetime.now(timezone.utc)
                    cancelled_tasks.append(tid)
        return {
            "ok": True,
            "phone": p,
            "contact_id": c.id,
            "name": c.name,
            "cancelled_tasks": cancelled_tasks,
        }


# --- Forward owner answer (Phase 1c.fix) ------------------------------------

def forward_owner_answer(contact_phone: str, answer_text: str) -> dict[str, Any]:
    """Reenvía la respuesta corta del owner (Owner) a un contacto que Iris
    consultó previamente (típicamente tras un `task_response_ack`).

    - NO crea task nueva.
    - NO incluye saludo de presentación ni pitch — el contacto ya conoce a Iris.
    - Usa el thread más reciente del contacto.
    - Envía vía wa-listener (mismo endpoint que send_outbound).
    - Persiste el Message con model_used='owner_answer_forward'.

    Returns {ok, thread_id?, contact_name?, error?, warning?}.
    """
    from .sessions import sanitize_phone

    if not contact_phone or not contact_phone.strip():
        return {"ok": False, "error": "contact_phone vacío"}
    if not answer_text or not answer_text.strip():
        return {"ok": False, "error": "answer_text vacío"}
    answer_text = answer_text.strip()
    if len(answer_text) > 1024:
        answer_text = answer_text[:1024]

    p = sanitize_phone(contact_phone)
    warning: str | None = None

    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            return {"ok": False, "error": f"contacto no existe para phone={p}"}
        contact_name = c.name
        contact_id = c.id
        # Thread más reciente
        thread = s.scalar(
            select(Thread)
            .where(Thread.contact_id == c.id)
            .order_by(Thread.opened_at.desc())
            .limit(1)
        )
        if thread is None:
            return {"ok": False, "error": "contacto sin thread previo — no hay conversación que continuar"}
        thread_id = thread.id

        # Verifica que exista un Message reciente (últimas 24h) con model_used='task_response_ack'.
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        ack_msg = s.scalar(
            select(Message)
            .where(
                Message.thread_id == thread_id,
                Message.model_used == "task_response_ack",
                Message.created_at >= cutoff,
            )
            .order_by(Message.created_at.desc())
            .limit(1)
        )
        if ack_msg is None:
            warning = (
                "no encontré task_response_ack reciente (<24h) en este thread; "
                "envío en modo fallback. Asegúrate de que esto es realmente "
                "una respuesta a algo que Iris reportó."
            )
            log.warning("forward_owner_answer fallback: phone=%s sin ack reciente", p)

    # POST a wa-listener (mismo path que send_outbound)
    wa_url = (settings.CONTACT_RELAY_WEBHOOK or "http://localhost:8099/send-to-contact")
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(wa_url, json={
                "type": "outbound",
                "phone": p,
                "body": answer_text,
                "thread_id": thread_id,
            })
            r.raise_for_status()
            wa_resp = r.json()
    except httpx.HTTPError as e:
        log.exception("forward_owner_answer: wa-listener falló")
        return {"ok": False, "error": str(e)}

    if not wa_resp.get("ok"):
        log.warning("forward_owner_answer: wa-listener returned %s", wa_resp)
        return {"ok": False, "error": wa_resp.get("error", "wa_listener_fail")}

    # Persiste Message como owner_answer_forward
    with get_session() as s:
        s.add(Message(
            thread_id=thread_id,
            direction=MessageDirection.out,
            body=answer_text,
            model_used="owner_answer_forward",
        ))

    log.info(
        "forward_owner_answer OK: contact_id=%s thread_id=%s len=%d",
        contact_id, thread_id, len(answer_text),
    )
    result: dict[str, Any] = {
        "ok": True,
        "thread_id": thread_id,
        "contact_name": contact_name,
        "contact_id": contact_id,
        "message_id": wa_resp.get("message_id"),
    }
    if warning:
        result["warning"] = warning
    return result


# --- Response tracking (called from chat.handle_message) --------------------

def render_message_template(template: str, contact_name: str | None, contact_phone: str) -> str:
    """Sustituye placeholders {{name}}, {{phone}}, {{first_name}} en el template.

    Si name es None usa 'amigo'/'amiga' genérico. first_name = primer token de name.
    """
    name = contact_name or "amigo"
    first = name.split()[0] if name else "amigo"
    return (
        template
        .replace("{{name}}", name)
        .replace("{{first_name}}", first)
        .replace("{{phone}}", contact_phone)
    )


def send_all_pending(task_id: int, message_template: str) -> dict[str, Any]:
    """Envía a TODOS los targets de la task que estén en status='pending'.

    Aplica render_message_template por cada target. Devuelve summary con ok/fail por target.
    """
    with get_session() as s:
        targets = list(
            s.scalars(
                select(TaskTarget).where(TaskTarget.task_id == task_id, TaskTarget.status == "pending")
            )
        )
        target_specs: list[dict] = []
        for tt in targets:
            c = s.get(Contact, tt.contact_id)
            target_specs.append({
                "target_id": tt.id,
                "contact_id": tt.contact_id,
                "contact_name": c.name if c else None,
                "contact_phone": c.phone if c else "",
            })

    results = []
    for spec in target_specs:
        body = render_message_template(message_template, spec["contact_name"], spec["contact_phone"])
        r = send_outbound(task_id, spec["target_id"], body)
        results.append({
            "target_id": spec["target_id"],
            "contact_name": spec["contact_name"],
            "ok": r.get("ok", False),
            "error": r.get("error"),
            "body": body if r.get("ok") else None,
        })
    sent_count = sum(1 for r in results if r["ok"])
    failed_count = len(results) - sent_count
    return {
        "ok": failed_count == 0,
        "sent": sent_count,
        "failed": failed_count,
        "results": results,
    }


def execute_task(task_id: int) -> dict[str, Any]:
    """Ejecuta una task pending: envía a todos los task_targets pending.

    Si task.context tiene `asset_id` (int) → usa send_outbound_media con
    context.caption como caption. Si no, usa send_outbound con
    context.message_template como body. Si tampoco hay template, falla con error.

    Después del envío masivo actualiza task.status:
      - todos sent → 'awaiting_responses' (send_outbound ya lo hace, pero
        si no había template/asset y nada se envió, dejamos 'pending').
      - parciales → 'in_progress'.
      - 0 enviados → 'pending' sin cambios.

    Devuelve {ok, sent, failed, errors, status}.
    """
    with get_session() as s:
        task = s.get(Task, task_id)
        if task is None:
            return {"ok": False, "error": "task no existe"}
        ctx = task.context or {}
        asset_id = ctx.get("asset_id")
        caption = ctx.get("caption")
        message_template = ctx.get("message_template")
        # Cargamos specs de targets pending para no mantener sesión abierta
        targets = list(
            s.scalars(
                select(TaskTarget).where(
                    TaskTarget.task_id == task_id, TaskTarget.status == "pending"
                )
            )
        )
        specs: list[dict] = []
        for tt in targets:
            c = s.get(Contact, tt.contact_id)
            specs.append({
                "target_id": tt.id,
                "contact_id": tt.contact_id,
                "contact_name": c.name if c else None,
                "contact_phone": c.phone if c else "",
            })

    if not specs:
        return {"ok": False, "error": "no hay targets pending", "sent": 0, "failed": 0}

    if not asset_id and not message_template:
        return {
            "ok": False,
            "error": "task.context no tiene asset_id ni message_template — no se puede ejecutar",
            "sent": 0,
            "failed": 0,
        }

    sent = 0
    failed = 0
    errors: list[dict] = []
    for spec in specs:
        try:
            if asset_id:
                # Caption opcional, render placeholders sobre nombre del target
                rendered_caption = (
                    render_message_template(caption, spec["contact_name"], spec["contact_phone"])
                    if caption else None
                )
                # Si también vino message_template, va como texto previo a la imagen
                rendered_body = (
                    render_message_template(message_template, spec["contact_name"], spec["contact_phone"])
                    if message_template else None
                )
                r = send_outbound_media(
                    task_id, spec["target_id"], int(asset_id),
                    caption=rendered_caption, body_text=rendered_body,
                )
            else:
                body = render_message_template(message_template, spec["contact_name"], spec["contact_phone"])
                r = send_outbound(task_id, spec["target_id"], body)
        except Exception as e:  # noqa: BLE001
            log.exception("execute_task: target_id=%s falló", spec["target_id"])
            failed += 1
            errors.append({"target_id": spec["target_id"], "error": str(e)})
            continue
        if r.get("ok"):
            sent += 1
        else:
            failed += 1
            errors.append({"target_id": spec["target_id"], "error": r.get("error", "unknown")})

    # Update task status final
    with get_session() as s:
        t = s.get(Task, task_id)
        if t is not None and t.status not in ("complete", "cancelled"):
            if failed == 0 and sent > 0:
                # send_outbound[_media] ya pone awaiting_responses si no quedan pending;
                # respetamos lo que dejaron.
                pass
            elif sent > 0 and failed > 0:
                t.status = "in_progress"
        final_status = t.status if t else None

    return {
        "ok": failed == 0,
        "sent": sent,
        "failed": failed,
        "errors": errors,
        "status": final_status,
        "task_id": task_id,
    }


def list_due_scheduled_task_ids(limit: int = 20) -> list[int]:
    """Devuelve ids de tasks pending con scheduled_at <= now() (worker)."""
    from sqlalchemy import func as sa_func
    with get_session() as s:
        stmt = (
            select(Task.id)
            .where(
                Task.status == "pending",
                Task.scheduled_at.is_not(None),
                Task.scheduled_at <= sa_func.now(),
            )
            .order_by(Task.scheduled_at)
            .limit(limit)
        )
        return list(s.scalars(stmt))


def classify_response(message_sent: str, response_text: str, anthropic_client=None) -> dict[str, Any]:
    """Clasifica la respuesta de un target a un outbound de Iris.

    Returns {classification, reasoning}. classification ∈
    {accepted, declined, maybe, clarify, other}.
    """
    import anthropic
    from .config import settings

    valid = {"accepted", "declined", "maybe", "clarify", "other"}
    if not response_text or not response_text.strip():
        return {"classification": "other", "reasoning": "empty response"}

    client = anthropic_client or anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    system_prompt = (
        "Clasifica la respuesta de un contacto a un mensaje outbound. Categorías:\n"
        "- accepted: confirma, acepta, dice sí, va, dale, perfecto\n"
        "- declined: rechaza, dice no, no puede, no podrá, no le queda\n"
        "- maybe: tentativo, condicional ('a ver', 'depende', 'creo que sí pero...')\n"
        "- clarify: pide más info, aclara, hace pregunta antes de comprometerse\n"
        "- other: cualquier otra cosa (saludo, comentario aleatorio, fuera de tema)\n\n"
        "Devuelve SOLO JSON: {\"classification\": \"<cat>\", \"reasoning\": \"<breve>\"}. Sin texto adicional."
    )
    user_msg = f"Mensaje enviado por el asistente:\n{message_sent}\n\nRespuesta del contacto:\n{response_text}"

    try:
        resp = client.messages.create(
            model=settings.IRIS_BRAIN_MODEL_DEFAULT,
            max_tokens=120,
            system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user_msg}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
        import json
        data = json.loads(raw)
        c = data.get("classification", "other")
        if c not in valid:
            c = "other"
        return {"classification": c, "reasoning": data.get("reasoning", "")}
    except Exception as e:  # noqa: BLE001
        log.exception("classify_response failed")
        return {"classification": "other", "reasoning": f"classifier error: {e}"}


def find_active_task_target(contact_id: int) -> dict[str, Any] | None:
    """Si este contacto tiene un task_target en status='sent' (esperando respuesta), devuélvelo como dict.

    Devuelve dict para evitar DetachedInstanceError al acceder atributos fuera de sesión.
    """
    with get_session() as s:
        stmt = (
            select(TaskTarget)
            .where(TaskTarget.contact_id == contact_id, TaskTarget.status == "sent")
            .order_by(TaskTarget.message_sent_at.desc())
            .limit(1)
        )
        tt = s.scalar(stmt)
        if tt is None:
            return None
        return {
            "id": tt.id,
            "task_id": tt.task_id,
            "contact_id": tt.contact_id,
            "message_sent": tt.message_sent,
            "message_sent_at": tt.message_sent_at.isoformat() if tt.message_sent_at else None,
        }


def classify_and_record_response(
    target_id: int,
    response_text: str,
    classification: str,
) -> dict[str, Any]:
    """Marca task_target como responded + classification. Si era la última pendiente, completa task."""
    valid = {"accepted", "declined", "maybe", "clarify", "other", "no_response"}
    if classification not in valid:
        classification = "other"
    now = datetime.now(timezone.utc)
    with get_session() as s:
        tt = s.get(TaskTarget, target_id)
        if tt is None:
            return {"ok": False, "error": "task_target no existe"}
        tt.response = response_text[:2000]
        tt.responded_at = now
        tt.response_classification = classification
        tt.status = "responded"
        task_id = tt.task_id
        s.flush()  # asegura que el UPDATE de tt.status se ve en queries siguientes

        # Si ya respondieron todos → task complete
        from sqlalchemy import func as sa_func
        still_waiting = s.scalar(
            select(sa_func.count())
            .select_from(TaskTarget)
            .where(TaskTarget.task_id == task_id, TaskTarget.status == "sent")
        ) or 0
        if still_waiting == 0:
            t = s.get(Task, task_id)
            if t is not None and t.status not in ("cancelled", "complete"):
                t.status = "complete"
                t.completed_at = now
                log.info("task %s → complete (todos los targets respondieron)", task_id)
        return {"ok": True, "task_id": task_id, "task_complete": still_waiting == 0}
