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
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import or_, select

from .config import settings
from .db import get_session
from .models import Contact, Task, TaskTarget, Thread, Message, MessageDirection

log = logging.getLogger("iris_brain.agentic")


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
        # Resolver/crear thread para este contacto (igual que sessions.open_or_get_thread)
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

def report_to_owner(text: str, task_id: int | None = None) -> dict[str, Any]:
    """Manda mensaje a Owner en Telegram via la API directa.

    No usa el relay-bot HTTP (sería circular). Va directo a Telegram bot API.
    """
    if not text or not text.strip():
        return {"ok": False, "error": "text vacío"}
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
        return {"ok": True}
    except httpx.HTTPError as e:
        log.exception("report_to_owner failed")
        return {"ok": False, "error": str(e)}


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
