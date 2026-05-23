"""FastAPI server para iris-brain. Puerto :8096."""
from __future__ import annotations

import logging
from typing import Any

from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from . import admin as admin_mod
from . import chat, sessions, soul
from .config import settings
from .db import get_session
from .models import (
    Contact,
    KbFact,
    KbFactSource,
    Message,
    MessageDirection,
    Ticket,
    TicketStatus,
)
from .relay import get_relay

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("iris_brain.server")

app = FastAPI(title="iris-brain", version="0.1.0")


@app.on_event("startup")
def _load_runtime_overrides() -> None:
    """Carga overrides DB → settings al iniciar."""
    try:
        from . import config as config_mod

        config_mod.load_overrides()
    except Exception:
        log.exception("no se pudieron cargar runtime overrides al boot")


# Background scheduler — corre tasks con scheduled_at <= now() cada 60s.
_scheduler_task: Any = None


async def _scheduler_loop() -> None:
    import asyncio
    from . import agentic
    log.info("scheduler loop iniciado (poll 60s)")
    while True:
        try:
            ids = agentic.list_due_scheduled_task_ids(limit=20)
            for tid in ids:
                log.info("scheduler: ejecutando task #%s (scheduled_at vencido)", tid)
                try:
                    r = agentic.execute_task(tid)
                    log.info("scheduler task #%s → sent=%s failed=%s", tid, r.get("sent"), r.get("failed"))
                except Exception:  # noqa: BLE001
                    log.exception("scheduler: execute_task %s falló", tid)
        except Exception:  # noqa: BLE001
            log.exception("scheduler loop iteration falló")
        await asyncio.sleep(60)


@app.on_event("startup")
async def _start_scheduler() -> None:
    import asyncio
    global _scheduler_task
    _scheduler_task = asyncio.create_task(_scheduler_loop())


@app.on_event("shutdown")
async def _stop_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task is not None:
        _scheduler_task.cancel()


# ---------------------------------------------------------------------------
# Admin auth dependency
# ---------------------------------------------------------------------------


def require_admin(x_iris_admin_token: str | None = Header(default=None)) -> None:
    if not admin_mod.check_admin_token(x_iris_admin_token):
        raise HTTPException(401, "admin token inválido o ausente")


class ChatRequest(BaseModel):
    contact_phone: str
    text: str
    media_url: str | None = None
    pushname: str | None = None
    real_phone: str | None = None


class JMFReplyRequest(BaseModel):
    ticket_id: int
    body: str


class KbFactUpsert(BaseModel):
    kb_slug: str
    key: str
    value: str
    source: KbFactSource = KbFactSource.jmf
    ttl_days: int | None = None


class ResetRequest(BaseModel):
    phone: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model_default": settings.IRIS_BRAIN_MODEL_DEFAULT,
        "model_safety": settings.IRIS_BRAIN_MODEL_SAFETY,
        "port": settings.IRIS_BRAIN_PORT,
        "soul_size": len(soul.soul_text()),
        "relay_configured": bool(settings.JMF_RELAY_WEBHOOK),
        "contact_relay_configured": bool(settings.CONTACT_RELAY_WEBHOOK),
    }


@app.post("/chat")
def chat_endpoint(req: ChatRequest) -> dict[str, Any]:
    if not req.text.strip() and not req.media_url:
        raise HTTPException(400, "text vacío y sin media_url")
    result = chat.handle_message(
        req.contact_phone,
        req.text,
        media_url=req.media_url,
        pushname=req.pushname,
    )
    if result.get("error"):
        raise HTTPException(500, result["error"])
    return result


@app.get("/tasks")
def list_tasks(status: str | None = None, limit: int = 50) -> dict[str, Any]:
    """Lista tasks agénticas con sus targets. status filter opcional."""
    from .models import Task, TaskTarget
    from sqlalchemy import func as sa_func

    with get_session() as s:
        q = select(Task).order_by(Task.updated_at.desc())
        if status:
            q = q.where(Task.status == status)
        q = q.limit(max(1, min(limit, 200)))
        rows = list(s.scalars(q))
        out = []
        for t in rows:
            tt_rows = list(
                s.scalars(select(TaskTarget).where(TaskTarget.task_id == t.id).order_by(TaskTarget.id))
            )
            targets = []
            for tt in tt_rows:
                c = s.get(Contact, tt.contact_id)
                targets.append({
                    "id": tt.id,
                    "contact_id": tt.contact_id,
                    "contact_name": c.name if c else None,
                    "contact_phone": c.phone if c else None,
                    "status": tt.status,
                    "message_sent": tt.message_sent,
                    "message_sent_at": tt.message_sent_at.isoformat() if tt.message_sent_at else None,
                    "response": tt.response,
                    "response_classification": tt.response_classification,
                    "responded_at": tt.responded_at.isoformat() if tt.responded_at else None,
                })
            owner = s.get(Contact, t.owner_id)
            out.append({
                "id": t.id,
                "owner_name": owner.name if owner else None,
                "kind": t.kind,
                "summary": t.summary,
                "raw_instruction": t.raw_instruction,
                "status": t.status,
                "context": t.context,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
                "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
                "targets": targets,
            })
        return {"tasks": out, "count": len(out)}


class TaskPreviewRequest(BaseModel):
    kind: str
    summary: str
    raw_instruction: str | None = None
    target_contact_ids: list[int]
    expected_names: list[str] | None = None
    message_template: str
    context: dict[str, Any] | None = None
    owner_phone: str | None = None


@app.post("/tasks/preview")
def preview_task(req: TaskPreviewRequest) -> dict[str, Any]:
    """Crea task agéntica en 'pending' y devuelve preview personalizado por target.

    NO envía. UI/Telegram presentan preview a Owner; tras confirmar → POST /tasks/{id}/send-all.
    """
    from . import agentic
    from .models import ContactKind

    with get_session() as s:
        if req.owner_phone:
            p = sessions.sanitize_phone(req.owner_phone)
            owner = s.scalar(select(Contact).where(Contact.phone == p, Contact.kind == ContactKind.owner))
        else:
            owner = s.scalar(select(Contact).where(Contact.kind == ContactKind.owner).limit(1))
        if owner is None:
            raise HTTPException(404, "no hay contacto kind='owner'")
        owner_id = owner.id

    r = agentic.create_task(
        owner_id=owner_id,
        kind=req.kind,
        summary=req.summary,
        raw_instruction=req.raw_instruction,
        target_contact_ids=req.target_contact_ids,
        context=req.context,
        expected_names=req.expected_names,
    )
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "create_task failed"))

    previews = []
    for tg in r["targets"]:
        body = agentic.render_message_template(
            req.message_template,
            tg.get("contact_name"),
            tg.get("contact_phone") or "",
        )
        previews.append({**tg, "preview_body": body})
    return {
        "ok": True,
        "task_id": r["task_id"],
        "targets": previews,
        "message_template": req.message_template,
        "auto_corrections": r.get("auto_corrections", []),
        "warning": r.get("warning"),
    }


class TaskSendAllRequest(BaseModel):
    message_template: str


@app.post("/tasks/{task_id}/send-all")
def send_all_task(task_id: int, req: TaskSendAllRequest) -> dict[str, Any]:
    """Envía a todos los targets pending con el message_template dado."""
    from . import agentic
    return agentic.send_all_pending(task_id, req.message_template)


class TaskPatchRequest(BaseModel):
    scheduled_at: str | None = None
    asset_id: int | None = None
    caption: str | None = None
    message_template: str | None = None


@app.post("/tasks/{task_id}/patch")
def patch_task(task_id: int, req: TaskPatchRequest) -> dict[str, Any]:
    """Patch parcial de una task — actualiza scheduled_at y/o context.asset_id/caption/message_template."""
    from .models import Task
    from datetime import timezone as _tz
    with get_session() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(404, "task no existe")
        if req.scheduled_at is not None:
            if req.scheduled_at == "":
                t.scheduled_at = None
            else:
                try:
                    sched_dt = datetime.fromisoformat(req.scheduled_at.replace("Z", "+00:00"))
                except ValueError as e:
                    raise HTTPException(400, f"scheduled_at inválido: {e}")
                if sched_dt.tzinfo is None:
                    sched_dt = sched_dt.replace(tzinfo=_tz.utc)
                t.scheduled_at = sched_dt
        ctx = dict(t.context or {})
        if req.asset_id is not None:
            ctx["asset_id"] = int(req.asset_id)
        if req.caption is not None:
            ctx["caption"] = req.caption
        if req.message_template is not None:
            ctx["message_template"] = req.message_template
        t.context = ctx
        return {"ok": True, "task_id": task_id, "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None, "context": ctx}


@app.post("/tasks/{task_id}/execute")
def execute_task_endpoint(task_id: int, force: bool = False) -> dict[str, Any]:
    """Ejecuta una task pending (envía a todos los targets pending).

    Si la task tiene `scheduled_at` futuro y `force=false`, devuelve 409 con info
    de la fecha programada. Pasa `?force=true` para anular el schedule.

    Usa task.context.asset_id+caption si presente (send_outbound_media),
    si no usa task.context.message_template (send_outbound).
    """
    from . import agentic
    from .models import Task

    if not force:
        with get_session() as s:
            t = s.get(Task, task_id)
            if t is None:
                raise HTTPException(404, "task no existe")
            if t.scheduled_at is not None:
                now = datetime.now(timezone.utc)
                sched = t.scheduled_at
                if sched.tzinfo is None:
                    sched = sched.replace(tzinfo=timezone.utc)
                if sched > now:
                    raise HTTPException(
                        409,
                        f"task #{task_id} está programada para {sched.isoformat()} "
                        f"— no se ejecuta hasta esa fecha (usa ?force=true para anular).",
                    )
    r = agentic.execute_task(task_id)
    if not r.get("ok") and r.get("error") and r.get("sent", 0) == 0 and r.get("failed", 0) == 0:
        # Error duro (no targets, sin template, task no existe)
        raise HTTPException(400, r["error"])
    return r


class TaskCreateRequest(BaseModel):
    kind: str
    summary: str
    raw_instruction: str | None = None
    target_contact_ids: list[int]
    expected_names: list[str] | None = None
    context: dict[str, Any] | None = None
    scheduled_at: str | None = None  # ISO 8601
    owner_phone: str | None = None


@app.post("/tasks")
def create_task_endpoint(req: TaskCreateRequest) -> dict[str, Any]:
    """Crea task desde UI (no envía). Persiste asset_id/caption/message_template en context y scheduled_at."""
    from . import agentic
    from .models import ContactKind, Task

    with get_session() as s:
        if req.owner_phone:
            p = sessions.sanitize_phone(req.owner_phone)
            owner = s.scalar(select(Contact).where(Contact.phone == p, Contact.kind == ContactKind.owner))
        else:
            owner = s.scalar(select(Contact).where(Contact.kind == ContactKind.owner).limit(1))
        if owner is None:
            raise HTTPException(404, "no hay contacto kind='owner'")
        owner_id = owner.id

    r = agentic.create_task(
        owner_id=owner_id,
        kind=req.kind,
        summary=req.summary,
        raw_instruction=req.raw_instruction,
        target_contact_ids=req.target_contact_ids,
        context=req.context,
        expected_names=req.expected_names,
    )
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "create_task failed"))

    # Si vino scheduled_at, parsear y persistir
    sched_iso = req.scheduled_at
    if sched_iso:
        try:
            sched_dt = datetime.fromisoformat(sched_iso.replace("Z", "+00:00"))
        except ValueError as e:
            raise HTTPException(400, f"scheduled_at inválido: {e}")
        with get_session() as s:
            t = s.get(Task, r["task_id"])
            if t is not None:
                # Asegurar tz-aware (si UI manda naive, asumir UTC)
                if sched_dt.tzinfo is None:
                    from datetime import timezone as _tz
                    sched_dt = sched_dt.replace(tzinfo=_tz.utc)
                t.scheduled_at = sched_dt
    return r


@app.post("/tasks/{task_id}/cancel")
def cancel_task(task_id: int) -> dict[str, Any]:
    from .models import Task, TaskTarget
    from sqlalchemy import update as sa_update

    with get_session() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(404, "task no existe")
        t.status = "cancelled"
        t.completed_at = datetime.now(timezone.utc)
        s.execute(
            sa_update(TaskTarget).where(TaskTarget.task_id == task_id, TaskTarget.status.in_(["pending", "sent"])).values(status="cancelled")
        )
        return {"ok": True, "task_id": task_id, "status": "cancelled"}


@app.post("/tasks/{task_id}/close")
def close_task(task_id: int) -> dict[str, Any]:
    """Marca task como completa (manualmente). Cancela los targets que sigan en 'pending'
    pero deja los 'sent' y 'responded' intactos. Usar cuando Owner terminó de atender la task
    fuera de Iris y solo quiere quitarla del board."""
    from .models import Task, TaskTarget
    from sqlalchemy import update as sa_update

    with get_session() as s:
        t = s.get(Task, task_id)
        if t is None:
            raise HTTPException(404, "task no existe")
        t.status = "complete"
        t.completed_at = datetime.now(timezone.utc)
        s.execute(
            sa_update(TaskTarget).where(TaskTarget.task_id == task_id, TaskTarget.status == "pending").values(status="cancelled")
        )
        return {"ok": True, "task_id": task_id, "status": "complete"}


@app.post("/tasks/{task_id}/targets/{target_id}/cancel")
def cancel_task_target(task_id: int, target_id: int) -> dict[str, Any]:
    """Cancela un único target de la task (Amaya, p.ej.) sin tocar a los demás.
    Si tras cancelarlo no quedan targets pending/sent, marca la task como complete."""
    from .models import Task, TaskTarget
    from sqlalchemy import func as sa_func

    with get_session() as s:
        tt = s.get(TaskTarget, target_id)
        if tt is None or tt.task_id != task_id:
            raise HTTPException(404, "target no existe en esta task")
        if tt.status in ("responded",):
            raise HTTPException(409, "ese target ya respondió — no se puede cancelar (cancela la task entera si quieres archivarla)")
        tt.status = "cancelled"
        # Si ya no quedan en pending/sent, cerrar la task automáticamente.
        remaining = s.scalar(
            select(sa_func.count()).select_from(TaskTarget)
            .where(TaskTarget.task_id == task_id, TaskTarget.status.in_(["pending", "sent"]))
        ) or 0
        if remaining == 0:
            t = s.get(Task, task_id)
            if t is not None and t.status not in ("complete", "cancelled"):
                t.status = "complete"
                t.completed_at = datetime.now(timezone.utc)
        return {"ok": True, "task_id": task_id, "target_id": target_id, "status": "cancelled", "remaining_active": remaining}


class OwnerInstructRequest(BaseModel):
    text: str
    source: str = "telegram"  # 'telegram' | 'whatsapp'
    owner_phone: str | None = None  # default: usar el owner registrado en DB


@app.post("/owner/instruct")
def owner_instruct(req: OwnerInstructRequest) -> dict[str, Any]:
    """Owner dicta una instrucción agéntica desde Telegram. Iris la procesa con tools agentic."""
    from .models import ContactKind

    # Resolver el owner (Owner). Si no se pasa owner_phone, buscamos el primer contacto kind=owner.
    with get_session() as s:
        if req.owner_phone:
            p = sessions.sanitize_phone(req.owner_phone)
            owner = s.scalar(select(Contact).where(Contact.phone == p, Contact.kind == ContactKind.owner))
        else:
            owner = s.scalar(select(Contact).where(Contact.kind == ContactKind.owner).limit(1))
        if owner is None:
            raise HTTPException(404, "no hay contacto registrado con kind='owner'")
        owner_phone = owner.phone

    # Reusa handle_message — el flujo owner_instruction lo detecta por contact.kind=='owner'
    # y carga las tools agentic (Phase 1b lo activa explícitamente en chat.py).
    result = chat.handle_message(
        owner_phone,
        req.text,
        media_url=None,
        pushname=None,
    )
    if result.get("error"):
        raise HTTPException(500, result["error"])
    return {**result, "source": req.source}


@app.post("/jmf/reply")
def jmf_reply(req: JMFReplyRequest) -> dict[str, Any]:
    """Owner responde a un ticket; mandamos la respuesta al contacto vía relay."""
    with get_session() as s:
        t = s.get(Ticket, req.ticket_id)
        if t is None:
            raise HTTPException(404, "ticket no existe")
        t.jmf_response = req.body
        t.status = TicketStatus.awaiting_patient
        thread_id = t.thread_id
    relay_result = get_relay().send_to_contact(thread_id, req.body)
    # Persistir la salida en messages también.
    sessions.append_message(thread_id, MessageDirection.out, req.body, model_used="jmf_manual")
    return {"ok": True, "ticket_id": req.ticket_id, "relay": relay_result}


def _ticket_dict(t: Ticket) -> dict[str, Any]:
    return {
        "id": t.id,
        "thread_id": t.thread_id,
        "kind": t.kind,
        "summary": t.summary,
        "status": t.status.value,
        "draft_for_jmf": t.draft_for_jmf,
        "jmf_response": t.jmf_response,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
    }


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int) -> dict[str, Any]:
    with get_session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404, "ticket no existe")
        d = _ticket_dict(t)
        last = s.scalar(
            select(Message)
            .where(Message.thread_id == t.thread_id)
            .order_by(Message.ts.desc())
        )
        d["last_message"] = (
            {
                "id": last.id,
                "direction": last.direction.value,
                "body": last.body,
                "ts": last.ts.isoformat() if last.ts else None,
            }
            if last is not None
            else None
        )
        return d


@app.post("/tickets/{ticket_id}/close")
def close_ticket(ticket_id: int) -> dict[str, Any]:
    from datetime import datetime, timezone
    with get_session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404, "ticket no existe")
        t.status = TicketStatus.closed
        t.updated_at = datetime.now(timezone.utc)
        return {"ok": True, "ticket_id": ticket_id, "status": t.status.value}


class TicketStatusUpdate(BaseModel):
    status: str


@app.post("/tickets/{ticket_id}/status")
def set_ticket_status(ticket_id: int, req: TicketStatusUpdate) -> dict[str, Any]:
    """Cambia el status arbitrariamente (open/awaiting_jmf/awaiting_patient/closed)."""
    from datetime import datetime, timezone
    try:
        new_status = TicketStatus(req.status)
    except ValueError:
        raise HTTPException(400, f"status inválido: {req.status}")
    with get_session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404, "ticket no existe")
        t.status = new_status
        t.updated_at = datetime.now(timezone.utc)
        return {"ok": True, "ticket_id": ticket_id, "status": new_status.value}


@app.get("/threads/{thread_id}/messages")
def list_thread_messages(thread_id: int, limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(limit, 200))
    with get_session() as s:
        rows = list(
            s.scalars(
                select(Message)
                .where(Message.thread_id == thread_id)
                .order_by(Message.ts.desc())
                .limit(limit)
            )
        )
        rows.reverse()
        return {
            "thread_id": thread_id,
            "messages": [
                {
                    "id": m.id,
                    "direction": m.direction.value,
                    "body": m.body,
                    "media_url": m.media_url,
                    "model_used": m.model_used,
                    "ts": m.ts.isoformat() if m.ts else None,
                }
                for m in rows
            ],
        }


@app.get("/tickets")
def list_tickets(status: str | None = None) -> dict[str, Any]:
    with get_session() as s:
        q = select(Ticket).order_by(Ticket.created_at.desc())
        if status:
            try:
                q = q.where(Ticket.status == TicketStatus(status))
            except ValueError:
                raise HTTPException(400, f"status inválido: {status}")
        rows = list(s.scalars(q))
        return {"tickets": [_ticket_dict(t) for t in rows]}


@app.get("/contacts")
def list_contacts(
    q: str | None = None,
    kind: str | None = None,
    page: int = 1,
    page_size: int = 50,
    sort: str = "name",
) -> dict[str, Any]:
    from .models import ContactKind
    from sqlalchemy import or_, func as sa_func

    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size

    with get_session() as s:
        stmt = select(Contact)
        count_stmt = select(sa_func.count()).select_from(Contact)
        if q:
            like = f"%{q}%"
            cond = or_(Contact.name.ilike(like), Contact.phone.ilike(like), Contact.notes.ilike(like))
            stmt = stmt.where(cond)
            count_stmt = count_stmt.where(cond)
        if kind:
            try:
                k = ContactKind(kind)
            except ValueError:
                raise HTTPException(400, f"kind inválido: {kind}")
            stmt = stmt.where(Contact.kind == k)
            count_stmt = count_stmt.where(Contact.kind == k)

        if sort == "recent":
            stmt = stmt.order_by(Contact.last_seen.desc().nullslast(), Contact.id.desc())
        elif sort == "threads":
            # Contactos con threads más recientes (los que están conversando ahora con Iris).
            # Usa max(messages.ts) por thread → contacto.
            from .models import Thread, Message
            latest_msg = (
                select(Message.thread_id, sa_func.max(Message.ts).label("last_msg_ts"))
                .group_by(Message.thread_id)
                .subquery()
            )
            latest_per_contact = (
                select(Thread.contact_id, sa_func.max(latest_msg.c.last_msg_ts).label("last_ts"))
                .join(latest_msg, latest_msg.c.thread_id == Thread.id)
                .group_by(Thread.contact_id)
                .subquery()
            )
            stmt = stmt.outerjoin(latest_per_contact, latest_per_contact.c.contact_id == Contact.id).order_by(
                latest_per_contact.c.last_ts.desc().nullslast(),
                Contact.id.desc(),
            )
        else:  # default: alfabético por name (los sin nombre van al final)
            stmt = stmt.order_by(
                sa_func.lower(sa_func.coalesce(Contact.name, "zzz")).asc(),
                Contact.phone.asc(),
            )
        stmt = stmt.limit(page_size).offset(offset)
        rows = list(s.scalars(stmt))
        total = s.scalar(count_stmt) or 0
        items = [
            {
                "id": c.id,
                "phone": c.phone,
                "name": c.name,
                "kind": c.kind.value if c.kind else "otro",
                "notes": c.notes,
                "last_seen": c.last_seen.isoformat() if c.last_seen else None,
            }
            for c in rows
        ]
        return {"items": items, "total": total, "page": page, "page_size": page_size}


class ContactCreate(BaseModel):
    phone: str
    name: str | None = None
    kind: str = "otro"
    notes: str | None = None


@app.post("/contacts")
def create_contact(req: ContactCreate) -> dict[str, Any]:
    """Crea contacto manualmente desde UI. Upsert si phone ya existe."""
    from .models import ContactKind

    p = sessions.sanitize_phone(req.phone)
    if not p:
        raise HTTPException(400, "phone vacío o inválido")
    try:
        k = ContactKind(req.kind)
    except ValueError:
        raise HTTPException(400, f"kind inválido: {req.kind}")
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            c = Contact(phone=p, name=req.name, kind=k, notes=req.notes)
            s.add(c)
        else:
            if req.name and not c.name:
                c.name = req.name
            if k != ContactKind.otro and c.kind == ContactKind.otro:
                c.kind = k
            if req.notes:
                c.notes = f"{c.notes}\n{req.notes}" if c.notes else req.notes
        s.flush()
        return {
            "ok": True,
            "id": c.id,
            "phone": c.phone,
            "name": c.name,
            "kind": c.kind.value,
            "notes": c.notes,
        }


class ContactSendDirect(BaseModel):
    body: str


@app.post("/contacts/{phone}/send-direct")
def send_direct_to_contact(phone: str, req: ContactSendDirect) -> dict[str, Any]:
    """Manual override — owner manda mensaje directo a un contacto via Iris's WhatsApp,
    sin pasar por el flujo agéntico. Persiste el mensaje en messages con model_used='manual_override'.
    """
    from .models import Thread

    if not req.body or not req.body.strip():
        raise HTTPException(400, "body vacío")
    body = req.body.strip()
    p = sessions.sanitize_phone(phone)
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            raise HTTPException(404, "contact no existe")
        thread = s.scalar(
            select(Thread).where(Thread.contact_id == c.id).order_by(Thread.opened_at.desc()).limit(1)
        )
        if thread is None:
            thread = Thread(contact_id=c.id, channel="whatsapp")
            s.add(thread)
            s.flush()
        thread_id = thread.id
        contact_phone = c.phone

    # Send via wa-listener
    import httpx
    wa_url = settings.CONTACT_RELAY_WEBHOOK or "http://localhost:8099/send-to-contact"
    try:
        with httpx.Client(timeout=10) as client:
            r = client.post(wa_url, json={
                "type": "manual_override",
                "phone": contact_phone,
                "body": body,
                "thread_id": thread_id,
            })
            r.raise_for_status()
            wa_resp = r.json()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"wa-listener falló: {e}")
    if not wa_resp.get("ok"):
        raise HTTPException(502, wa_resp.get("error", "wa_listener_fail"))

    # Persist message
    sessions.append_message(thread_id, MessageDirection.out, body, model_used="manual_override")
    return {"ok": True, "message_id": wa_resp.get("message_id"), "thread_id": thread_id}


class TicketUpdate(BaseModel):
    kind: str | None = None
    summary: str | None = None
    draft_for_owner: str | None = None


@app.put("/tickets/{ticket_id}")
def update_ticket(ticket_id: int, req: TicketUpdate) -> dict[str, Any]:
    """Edita kind/summary/draft del ticket."""
    with get_session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404, "ticket no existe")
        if req.kind is not None:
            t.kind = req.kind
        if req.summary is not None:
            t.summary = req.summary
        if req.draft_for_owner is not None:
            t.draft_for_jmf = req.draft_for_owner  # column name is draft_for_jmf in private (historical)
        return {"ok": True, "ticket_id": ticket_id}


@app.delete("/tickets/{ticket_id}")
def delete_ticket(ticket_id: int) -> dict[str, Any]:
    """Borra el ticket permanentemente."""
    from sqlalchemy import text as sa_text
    with get_session() as s:
        r = s.execute(sa_text("DELETE FROM tickets WHERE id = :id"), {"id": ticket_id})
        if r.rowcount == 0:
            raise HTTPException(404, "ticket no existe")
        return {"ok": True, "deleted": r.rowcount}


class ContactUpdate(BaseModel):
    name: str | None = None
    kind: str | None = None
    notes: str | None = None


@app.put("/contacts/{phone}")
def update_contact_endpoint(phone: str, req: ContactUpdate) -> dict[str, Any]:
    from .models import ContactKind

    p = sessions.sanitize_phone(phone)
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            raise HTTPException(404, "contact no existe")
        if req.name is not None:
            c.name = req.name.strip() or None
        if req.kind is not None:
            try:
                c.kind = ContactKind(req.kind)
            except ValueError:
                raise HTTPException(400, f"kind inválido: {req.kind}")
        if req.notes is not None:
            c.notes = req.notes.strip() or None
        s.flush()
        return {
            "ok": True,
            "id": c.id,
            "phone": c.phone,
            "name": c.name,
            "kind": c.kind.value,
            "notes": c.notes,
        }


@app.delete("/contacts/{phone}")
def delete_contact_endpoint(phone: str) -> dict[str, Any]:
    from sqlalchemy import text as sa_text

    p = sessions.sanitize_phone(phone)
    with get_session() as s:
        # DELETE directo en SQL para que el ON DELETE CASCADE de la DB haga su trabajo
        # (el ORM SQLAlchemy intentaría nullear los FKs primero, fallando con NOT NULL).
        result = s.execute(sa_text("DELETE FROM contacts WHERE phone = :p"), {"p": p})
        if result.rowcount == 0:
            raise HTTPException(404, "contact no existe")
        return {"ok": True, "phone": p, "deleted": result.rowcount}


@app.get("/contacts/{phone}")
def get_contact(phone: str) -> dict[str, Any]:
    from .models import Thread, Message, Ticket

    p = sessions.sanitize_phone(phone)
    with get_session() as s:
        c = s.scalar(select(Contact).where(Contact.phone == p))
        if c is None:
            raise HTTPException(404, "contact no existe")
        contact_data = {
            "id": c.id,
            "phone": c.phone,
            "name": c.name,
            "kind": c.kind.value,
            "notes": c.notes,
            "first_seen": c.first_seen.isoformat() if c.first_seen else None,
            "last_seen": c.last_seen.isoformat() if c.last_seen else None,
        }
        # Último thread del contacto
        thread = s.scalar(
            select(Thread).where(Thread.contact_id == c.id).order_by(Thread.opened_at.desc()).limit(1)
        )
        thread_data = None
        messages_data: list[dict] = []
        if thread is not None:
            thread_data = {
                "id": thread.id,
                "status": thread.status.value if thread.status else None,
                "opened_at": thread.opened_at.isoformat() if thread.opened_at else None,
            }
            msg_rows = list(
                s.scalars(
                    select(Message).where(Message.thread_id == thread.id).order_by(Message.ts.asc()).limit(100)
                )
            )
            messages_data = [
                {
                    "id": m.id,
                    "direction": m.direction.value,
                    "body": m.body,
                    "ts": m.ts.isoformat() if m.ts else None,
                    "model_used": m.model_used,
                    "media_asset_id": m.media_asset_id,
                    "media_caption": m.media_caption,
                }
                for m in msg_rows
            ]
        # Tickets del contacto (via thread)
        tickets_data: list[dict] = []
        if thread is not None:
            ticket_rows = list(
                s.scalars(
                    select(Ticket).where(Ticket.thread_id == thread.id).order_by(Ticket.created_at.desc()).limit(20)
                )
            )
            tickets_data = [
                {
                    "id": t.id,
                    "kind": t.kind,
                    "summary": t.summary,
                    "status": t.status.value if t.status else None,
                    "created_at": t.created_at.isoformat() if t.created_at else None,
                }
                for t in ticket_rows
            ]
        return {
            "contact": contact_data,
            "thread": thread_data,
            "messages": messages_data,
            "tickets": tickets_data,
            # Compat con UI vieja que esperaba campos planos:
            **contact_data,
        }


@app.post("/kb-facts")
def upsert_kb_fact(req: KbFactUpsert) -> dict[str, Any]:
    with get_session() as s:
        cf = s.scalar(
            select(KbFact).where(
                KbFact.kb_slug == req.kb_slug,
                KbFact.key == req.key,
            )
        )
        if cf is None:
            cf = KbFact(
                kb_slug=req.kb_slug,
                key=req.key,
                value=req.value,
                source=req.source,
                ttl_days=req.ttl_days,
                version=1,
            )
            s.add(cf)
        else:
            cf.value = req.value
            cf.source = req.source
            cf.ttl_days = req.ttl_days
            cf.version = cf.version + 1
        s.flush()
        return {"ok": True, "id": cf.id, "version": cf.version}


@app.get("/kb-facts")
def list_kb_facts(kb_slug: str | None = None) -> dict[str, Any]:
    with get_session() as s:
        q = select(KbFact).order_by(KbFact.kb_slug, KbFact.key)
        if kb_slug:
            q = q.where(KbFact.kb_slug == kb_slug)
        rows = list(s.scalars(q))
        return {
            "kb_facts": [
                {
                    "id": cf.id,
                    "kb_slug": cf.kb_slug,
                    "slug": cf.kb_slug,  # alias para UI
                    "key": cf.key,
                    "value": cf.value,
                    "source": cf.source.value,
                    "version": cf.version,
                    "ttl_days": cf.ttl_days,
                    "updated_at": cf.updated_at.isoformat() if cf.updated_at else None,
                }
                for cf in rows
            ]
        }


@app.post("/reset")
def reset_endpoint(req: ResetRequest) -> dict[str, Any]:
    """Cierra threads abiertos. Si phone se da, solo de ese contacto; si no, todos."""
    from .models import Thread, ThreadStatus
    closed = 0
    with get_session() as s:
        q = select(Thread).where(Thread.status == ThreadStatus.open)
        if req.phone:
            p = sessions.sanitize_phone(req.phone)
            c = s.scalar(select(Contact).where(Contact.phone == p))
            if c is None:
                return {"ok": True, "closed": 0, "reason": "contact no existe"}
            q = q.where(Thread.contact_id == c.id)
        for t in s.scalars(q):
            t.status = ThreadStatus.closed
            closed += 1
    return {"ok": True, "closed": closed}


# ---------------------------------------------------------------------------
# Media endpoints (Phase 1c) — imágenes híbridas en tasks agénticas
# ---------------------------------------------------------------------------


class MediaIngestUrlRequest(BaseModel):
    url: str
    label: str | None = None
    tags: list[str] | None = None
    source: str = "marketing"


@app.post("/media/ingest-url")
def media_ingest_url(req: MediaIngestUrlRequest) -> dict[str, Any]:
    from . import media as media_mod
    try:
        return media_mod.ingest_from_url(
            req.url, label=req.label, tags=req.tags, source=req.source
        )
    except media_mod.MediaError as e:
        code_to_status = {
            "not_whitelisted": 403,
            "bad_mime": 400,
            "bad_source": 400,
            "too_large": 413,
            "fetch_failed": 502,
            "empty": 400,
        }
        raise HTTPException(code_to_status.get(e.code, 400), str(e))


@app.post("/media/upload")
async def media_upload(
    file: UploadFile = File(...),
    source: str = Form("ui_upload"),
    label: str | None = Form(None),
    tags: str | None = Form(None),  # JSON-encoded list o coma-separados
) -> dict[str, Any]:
    from . import media as media_mod
    import json as _json

    data = await file.read()
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if not mime:
        # Fallback por extensión
        name = (file.filename or "").lower()
        if name.endswith((".jpg", ".jpeg")):
            mime = "image/jpeg"
        elif name.endswith(".png"):
            mime = "image/png"
        elif name.endswith(".webp"):
            mime = "image/webp"
        elif name.endswith(".pdf"):
            mime = "application/pdf"

    parsed_tags: list[str] | None = None
    if tags:
        try:
            parsed = _json.loads(tags)
            if isinstance(parsed, list):
                parsed_tags = [str(x) for x in parsed]
        except Exception:
            parsed_tags = [t.strip() for t in tags.split(",") if t.strip()]

    try:
        return media_mod.ingest_from_bytes(
            data=data,
            mime_type=mime,
            source=source,
            label=label,
            tags=parsed_tags,
            filename_hint=file.filename,
        )
    except media_mod.MediaError as e:
        code_to_status = {
            "bad_mime": 400,
            "bad_source": 400,
            "too_large": 413,
            "empty": 400,
        }
        raise HTTPException(code_to_status.get(e.code, 400), str(e))


@app.get("/media/{asset_id}/raw")
def media_raw(asset_id: int):
    from . import media as media_mod
    info = media_mod.get_storage_path(asset_id)
    if info is None:
        raise HTTPException(404, "media no existe o fue borrado")
    storage_path, mime_type, filename = info
    return FileResponse(storage_path, media_type=mime_type, filename=filename)


@app.get("/media/{asset_id}")
def media_metadata(asset_id: int) -> dict[str, Any]:
    from . import media as media_mod
    m = media_mod.get_media(asset_id)
    if m is None:
        raise HTTPException(404, "media no existe")
    return m


@app.get("/media")
def media_search(q: str | None = None, source: str | None = None, limit: int = 50) -> dict[str, Any]:
    from . import media as media_mod
    return media_mod.find_media(q or "", limit=limit, source=source)


@app.delete("/media/{asset_id}")
def media_delete(asset_id: int) -> dict[str, Any]:
    from . import media as media_mod
    r = media_mod.soft_delete(asset_id)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "not_found"))
    return r


# ---------------------------------------------------------------------------
# Admin endpoints (Sprint 3) — todos requieren X-Iris-Admin-Token
# ---------------------------------------------------------------------------


class AdminConfigUpdate(BaseModel):
    model_default: str | None = None
    model_safety: str | None = None
    max_tokens: int | None = None
    thinking: str | None = None
    effort: str | None = None
    prompt_caching_enabled: str | None = None


class RotateKeyRequest(BaseModel):
    api_key: str


class SoulPutRequest(BaseModel):
    text: str


class TicketReplyRequest(BaseModel):
    body: str
    close_after: bool = False


class TicketReassignRequest(BaseModel):
    kind: str


@app.get("/admin/config", dependencies=[Depends(require_admin)])
def admin_get_config() -> dict[str, Any]:
    return admin_mod.get_config_snapshot()


@app.put("/admin/config", dependencies=[Depends(require_admin)])
def admin_put_config(req: AdminConfigUpdate) -> dict[str, Any]:
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    result = admin_mod.update_config(updates, updated_by="admin")
    if not result["ok"]:
        raise HTTPException(400, {"errors": result["errors"], "accepted": result["accepted"]})
    return {**result, "snapshot": admin_mod.get_config_snapshot()}


@app.post("/admin/reload-config", dependencies=[Depends(require_admin)])
def admin_reload_config() -> dict[str, Any]:
    return admin_mod.reload_config()


@app.post("/admin/rotate-key", dependencies=[Depends(require_admin)])
def admin_rotate_key(req: RotateKeyRequest) -> dict[str, Any]:
    try:
        return admin_mod.rotate_key(req.api_key, updated_by="admin")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(502, str(e))


@app.get("/admin/soul", dependencies=[Depends(require_admin)])
def admin_get_soul() -> dict[str, Any]:
    return admin_mod.get_soul()


@app.put("/admin/soul", dependencies=[Depends(require_admin)])
def admin_put_soul(req: SoulPutRequest) -> dict[str, Any]:
    try:
        return admin_mod.put_soul(req.text, updated_by="admin")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/admin/soul/reload", dependencies=[Depends(require_admin)])
def admin_reload_soul() -> dict[str, Any]:
    return admin_mod.reload_soul()


@app.get("/admin/tickets/live", dependencies=[Depends(require_admin)])
def admin_tickets_live(since: str | None = None) -> dict[str, Any]:
    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, f"since inválido (ISO 8601 esperado): {since!r}")
    return admin_mod.tickets_live(since=since_dt)


@app.post("/admin/tickets/{ticket_id}/reply", dependencies=[Depends(require_admin)])
def admin_ticket_reply(ticket_id: int, req: TicketReplyRequest) -> dict[str, Any]:
    """Wrap de /jmf/reply con close_after opcional."""
    result = jmf_reply(JMFReplyRequest(ticket_id=ticket_id, body=req.body))
    if req.close_after:
        try:
            admin_mod.close_ticket(ticket_id)
            result["closed"] = True
        except LookupError as e:
            raise HTTPException(404, str(e))
    return result


@app.post("/admin/tickets/{ticket_id}/reassign-kind", dependencies=[Depends(require_admin)])
def admin_ticket_reassign(ticket_id: int, req: TicketReassignRequest) -> dict[str, Any]:
    try:
        return admin_mod.reassign_ticket_kind(ticket_id, req.kind)
    except LookupError as e:
        raise HTTPException(404, str(e))


@app.get("/admin/metrics/today", dependencies=[Depends(require_admin)])
def admin_metrics_today() -> dict[str, Any]:
    return admin_mod.metrics_today()


from fastapi import Request as _FastAPIRequest  # noqa: E402


@app.get("/admin/metrics/range", dependencies=[Depends(require_admin)])
def admin_metrics_range(request: _FastAPIRequest) -> dict[str, Any]:
    """Query params: ?from=<ISO>&to=<ISO>. `from` es palabra reservada en Python,
    así que leemos directamente del request."""
    qp = request.query_params
    from_param = qp.get("from")
    to_param = qp.get("to")
    if not from_param or not to_param:
        raise HTTPException(400, "from y to son obligatorios (ISO 8601)")
    try:
        start = datetime.fromisoformat(from_param.replace("Z", "+00:00"))
        end = datetime.fromisoformat(to_param.replace("Z", "+00:00"))
    except ValueError as e:
        raise HTTPException(400, f"fecha inválida: {e}")
    return admin_mod.metrics_range(start, end)


@app.get("/admin/health/components", dependencies=[Depends(require_admin)])
def admin_health_components() -> dict[str, Any]:
    return admin_mod.health_components()


# ---------------------------------------------------------------------------
# Anti-saturación + control panel /iris (Feature 1,3,4)
# ---------------------------------------------------------------------------


class ForwardAnswerRequest(BaseModel):
    contact_phone: str
    answer_text: str


@app.post("/owner/forward-answer")
def owner_forward_answer(req: ForwardAnswerRequest) -> dict[str, Any]:
    """Reenvía la respuesta del owner (Owner) a un contacto. Llamado desde relay-bot
    cuando Owner aprieta los botones rápidos del menú usr:* o escribe en pending_reply."""
    from . import agentic
    r = agentic.forward_owner_answer(
        contact_phone=req.contact_phone,
        answer_text=req.answer_text,
    )
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "forward_owner_answer failed"))
    return r


class SilenceContactRequest(BaseModel):
    on: bool = True


@app.post("/contacts/{phone}/silence")
def silence_contact_endpoint(phone: str, req: SilenceContactRequest | None = None) -> dict[str, Any]:
    from . import agentic
    on = True if req is None else bool(req.on)
    r = agentic.silence_contact(phone, on=on)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "silence failed"))
    return r


@app.post("/contacts/{phone}/close-conversation")
def close_conversation_endpoint(phone: str) -> dict[str, Any]:
    from . import agentic
    r = agentic.close_conversation_with_contact(phone)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "close failed"))
    return r


class PauseContactRequest(BaseModel):
    hours: int


@app.post("/contacts/{phone}/pause")
def pause_contact_endpoint(phone: str, req: PauseContactRequest) -> dict[str, Any]:
    from . import agentic
    r = agentic.pause_contact(phone, hours=req.hours)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "pause failed"))
    return r


class IrisPauseRequest(BaseModel):
    duration: str  # "24h" | "7d" | "off"


@app.post("/iris/pause")
def iris_pause(req: IrisPauseRequest) -> dict[str, Any]:
    """Pausa global de Iris (no contesta a usuarios no-owner). Persiste en runtime_config."""
    dur = (req.duration or "").lower().strip()
    if dur in {"off", "resume", "none", ""}:
        admin_mod._set_runtime("iris_paused_until", "", updated_by="telegram_menu")
        return {"ok": True, "paused_until": None}
    units = {"h": 1, "d": 24}
    try:
        n = int(dur[:-1])
        unit = dur[-1]
        if unit not in units:
            raise ValueError
        hours = n * units[unit]
    except (ValueError, IndexError):
        raise HTTPException(400, f"duration inválido: {req.duration} (usa '24h', '7d', 'off')")
    pu = datetime.now(timezone.utc) + timedelta(hours=hours)
    admin_mod._set_runtime("iris_paused_until", pu.isoformat(), updated_by="telegram_menu")
    return {"ok": True, "paused_until": pu.isoformat()}


@app.get("/iris/status")
def iris_status() -> dict[str, Any]:
    paused = admin_mod._get_runtime("iris_paused_until")
    silent_global = (admin_mod._get_runtime("iris_silent_mode_global") or "").lower() == "true"
    paused_dt = None
    if paused:
        try:
            paused_dt = datetime.fromisoformat(paused.replace("Z", "+00:00"))
        except ValueError:
            paused_dt = None
    is_paused = bool(paused_dt and paused_dt > datetime.now(timezone.utc))
    return {
        "ok": True,
        "paused_until": paused if is_paused else None,
        "is_paused": is_paused,
        "silent_mode_global": silent_global,
    }


@app.post("/iris/silent-mode-toggle")
def iris_silent_mode_toggle() -> dict[str, Any]:
    cur = (admin_mod._get_runtime("iris_silent_mode_global") or "").lower() == "true"
    new = not cur
    admin_mod._set_runtime("iris_silent_mode_global", "true" if new else "false", updated_by="telegram_menu")
    return {"ok": True, "silent_mode_global": new}


@app.get("/iris/active-conversations")
def iris_active_conversations(limit: int = 20) -> dict[str, Any]:
    """Resumen para el botón 'Conversaciones activas' del /iris control panel."""
    from .models import Task, TaskTarget
    with get_session() as s:
        rows = list(
            s.scalars(
                select(Task)
                .where(Task.status.in_(["pending", "in_progress", "awaiting_responses"]))
                .order_by(Task.updated_at.desc())
                .limit(max(1, min(limit, 100)))
            )
        )
        out = []
        for t in rows:
            contacts_for_task: list[str] = []
            tt_rows = list(s.scalars(select(TaskTarget).where(TaskTarget.task_id == t.id)))
            for tt in tt_rows:
                c = s.get(Contact, tt.contact_id)
                if c is not None:
                    contacts_for_task.append(c.name or c.phone or f"#{c.id}")
            out.append({
                "task_id": t.id,
                "summary": t.summary,
                "status": t.status,
                "contacts": contacts_for_task,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None,
            })
    return {"tasks": out, "count": len(out)}


# ---------------------------------------------------------------------------


def main() -> None:
    import uvicorn
    log.info("starting iris-brain :%d model=%s", settings.IRIS_BRAIN_PORT, settings.IRIS_BRAIN_MODEL_DEFAULT)
    uvicorn.run(app, host=settings.IRIS_BRAIN_HOST, port=settings.IRIS_BRAIN_PORT, log_level="info")


if __name__ == "__main__":
    main()
