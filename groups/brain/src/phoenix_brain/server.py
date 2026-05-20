import json
from dataclasses import asdict
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, func, select

from . import app_config
from . import soul as soul_mod
from .chat import ChatRequest, handle_chat
from .config import settings, top
from .db import create_all, get_session
from .discussions import start_discussion_api
from .models import (
    GROUP_MODES,
    AuditLog,
    DiscussionStarter,
    Group,
    GroupKb,
    GroupSoul,
    KbFact,
    KnowledgeBase,
    Message,
)

app = FastAPI(title="phoenix-brain", version="0.1.0")


# ── Schemas ─────────────────────────────────────────────────────────
class DiscussionIn(BaseModel):
    group_jid: str
    topic: str
    auto_publish: bool = False


class ChatIn(BaseModel):
    group_jid: Optional[str] = None
    contact_jid: Optional[str] = None
    contact_name: Optional[str] = None
    text: str
    media_hint: Optional[str] = None
    mentions_phoenix: bool = False
    quoted_msg_id: Optional[str] = None
    quoted_is_phoenix: bool = False


class ModeIn(BaseModel):
    mode: str


class GroupNameIn(BaseModel):
    display_name: str


class SoulIn(BaseModel):
    soul_md: str


class KbIn(BaseModel):
    slug: str
    name: str
    description: Optional[str] = ""


class FactIn(BaseModel):
    key: str
    value: str


class SubscribeIn(BaseModel):
    kb_slug: str
    priority: int = 0


class SettingsPatchIn(BaseModel):
    owner_jid: Optional[str] = None
    proactive_threshold: Optional[float] = None
    proactive_cooldown_min: Optional[int] = None


class DefaultSoulIn(BaseModel):
    soul_md: str


# ── Lifecycle ───────────────────────────────────────────────────────
@app.on_event("startup")
def _startup() -> None:
    create_all()


# ── Health ──────────────────────────────────────────────────────────
@app.get("/health")
def health() -> dict:
    with get_session() as s:
        n_groups = s.execute(select(func.count(Group.id))).scalar() or 0
        n_kbs = s.execute(select(func.count(KnowledgeBase.id))).scalar() or 0
        n_facts = s.execute(select(func.count(KbFact.id)).where(KbFact.status == "active")).scalar() or 0
        n_pending = s.execute(select(func.count(KbFact.id)).where(KbFact.status == "pending_review")).scalar() or 0
        n_messages_24h = s.execute(
            select(func.count(Message.id)).where(Message.ts >= func.datetime("now", "-1 day"))
        ).scalar() or 0
        n_drafts = s.execute(select(func.count(DiscussionStarter.id)).where(DiscussionStarter.status == "draft")).scalar() or 0
    return {
        "status": "ok",
        "model_default": settings.model_default,
        "model_safety": settings.model_safety,
        "model_proactive": settings.model_proactive,
        "owner_jid_set": bool(top.phoenix_owner_jid),
        "anthropic_key_set": bool(top.anthropic_api_key),
        "counts": {
            "groups": n_groups,
            "kbs": n_kbs,
            "facts_active": n_facts,
            "facts_pending": n_pending,
            "messages_24h": n_messages_24h,
            "drafts_pending": n_drafts,
        },
    }


# ── Chat (listener) ─────────────────────────────────────────────────
@app.post("/chat")
def chat(payload: ChatIn) -> dict:
    try:
        resp = handle_chat(ChatRequest(**payload.model_dump()))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {
        "reply": resp.reply,
        "model": resp.model,
        "usage": resp.usage,
        "gating": asdict(resp.gating),
        "tool_calls": resp.tool_calls,
    }


# ── Groups ──────────────────────────────────────────────────────────
def _serialize_group(g: Group, *, with_counts: bool = True, s=None) -> dict:
    out = {
        "wa_jid": g.wa_jid,
        "display_name": g.display_name,
        "mode": g.mode,
        "is_active": g.is_active,
        "notes": g.notes,
        "joined_at": g.joined_at.isoformat() if g.joined_at else None,
        "last_proactive_at": g.last_proactive_at.isoformat() if g.last_proactive_at else None,
    }
    if with_counts and s is not None:
        out["counts"] = {
            "messages": s.execute(select(func.count(Message.id)).where(Message.group_id == g.id)).scalar() or 0,
            "kbs": s.execute(select(func.count(GroupKb.id)).where(GroupKb.group_id == g.id)).scalar() or 0,
        }
    return out


@app.get("/groups")
def list_groups() -> list[dict]:
    with get_session() as s:
        rows = s.execute(select(Group).order_by(Group.joined_at.desc())).scalars().all()
        return [_serialize_group(g, s=s) for g in rows]


@app.get("/groups/{wa_jid:path}")
def get_group(wa_jid: str) -> dict:
    with get_session() as s:
        g = s.execute(select(Group).where(Group.wa_jid == wa_jid)).scalar_one_or_none()
        if not g:
            raise HTTPException(404, "group not found")
        soul = s.execute(
            select(GroupSoul).where(GroupSoul.group_id == g.id, GroupSoul.is_active == True)  # noqa: E712
            .order_by(GroupSoul.version.desc())
        ).scalar_one_or_none()
        kbs = s.execute(
            select(KnowledgeBase, GroupKb.priority)
            .join(GroupKb, GroupKb.kb_id == KnowledgeBase.id)
            .where(GroupKb.group_id == g.id)
            .order_by(GroupKb.priority.desc())
        ).all()
        return {
            **_serialize_group(g, s=s),
            "soul": {
                "version": soul.version if soul else 0,
                "updated_at": soul.updated_at.isoformat() if soul and soul.updated_at else None,
                "soul_md": soul.soul_md if soul else "",
            },
            "kbs": [
                {"slug": kb.slug, "name": kb.name, "description": kb.description, "priority": prio}
                for kb, prio in kbs
            ],
        }


@app.delete("/groups/{wa_jid:path}")
def delete_group(wa_jid: str) -> dict:
    """Borra grupo + SOULs + suscripciones a KBs + mensajes (cascada)."""
    with get_session() as s:
        g = s.execute(select(Group).where(Group.wa_jid == wa_jid)).scalar_one_or_none()
        if not g:
            raise HTTPException(404, "group not found")
        # Mensajes no tienen cascade explícito; los borramos antes para no quedar huérfanos.
        from .models import Message as _M
        s.execute(delete(_M).where(_M.group_id == g.id))
        s.delete(g)
        s.commit()
    soul_mod.invalidate_cache(wa_jid)
    return {"status": "ok", "deleted": wa_jid}


@app.patch("/groups/{wa_jid:path}/name")
def patch_group_name(wa_jid: str, payload: GroupNameIn) -> dict:
    name = payload.display_name.strip()
    if not name:
        raise HTTPException(400, "display_name no puede estar vacío")
    with get_session() as s:
        g = s.execute(select(Group).where(Group.wa_jid == wa_jid)).scalar_one_or_none()
        if not g:
            raise HTTPException(404, "group not found")
        g.display_name = name
        s.commit()
    return {"status": "ok", "wa_jid": wa_jid, "display_name": name}


@app.patch("/groups/{wa_jid:path}/mode")
def patch_group_mode(wa_jid: str, payload: ModeIn) -> dict:
    if payload.mode not in GROUP_MODES:
        raise HTTPException(400, f"invalid mode (must be one of {GROUP_MODES})")
    with get_session() as s:
        g = s.execute(select(Group).where(Group.wa_jid == wa_jid)).scalar_one_or_none()
        if not g:
            raise HTTPException(404, "group not found")
        g.mode = payload.mode
        s.commit()
    soul_mod.invalidate_cache(wa_jid)
    return {"status": "ok", "wa_jid": wa_jid, "mode": payload.mode}


@app.put("/groups/{wa_jid:path}/soul")
def put_group_soul(wa_jid: str, payload: SoulIn) -> dict:
    with get_session() as s:
        g = s.execute(select(Group).where(Group.wa_jid == wa_jid)).scalar_one_or_none()
        if not g:
            raise HTTPException(404, "group not found")
        active = s.execute(
            select(GroupSoul).where(GroupSoul.group_id == g.id, GroupSoul.is_active == True)  # noqa: E712
        ).scalar_one_or_none()
        new_version = (active.version + 1) if active else 1
        if active:
            active.is_active = False
        s.add(GroupSoul(group_id=g.id, soul_md=payload.soul_md, version=new_version, is_active=True))
        s.commit()
    soul_mod.invalidate_cache(wa_jid)
    return {"status": "ok", "wa_jid": wa_jid, "version": new_version}


@app.post("/groups/{wa_jid:path}/kbs")
def subscribe_kb(wa_jid: str, payload: SubscribeIn) -> dict:
    with get_session() as s:
        g = s.execute(select(Group).where(Group.wa_jid == wa_jid)).scalar_one_or_none()
        if not g:
            raise HTTPException(404, "group not found")
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == payload.kb_slug)).scalar_one_or_none()
        if not kb:
            raise HTTPException(404, "kb not found")
        existing = s.execute(
            select(GroupKb).where(GroupKb.group_id == g.id, GroupKb.kb_id == kb.id)
        ).scalar_one_or_none()
        if existing:
            existing.priority = payload.priority
        else:
            s.add(GroupKb(group_id=g.id, kb_id=kb.id, priority=payload.priority))
        s.commit()
    return {"status": "ok"}


@app.delete("/groups/{wa_jid:path}/kbs/{kb_slug}")
def unsubscribe_kb(wa_jid: str, kb_slug: str) -> dict:
    with get_session() as s:
        g = s.execute(select(Group).where(Group.wa_jid == wa_jid)).scalar_one_or_none()
        if not g:
            raise HTTPException(404, "group not found")
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == kb_slug)).scalar_one_or_none()
        if not kb:
            raise HTTPException(404, "kb not found")
        existing = s.execute(
            select(GroupKb).where(GroupKb.group_id == g.id, GroupKb.kb_id == kb.id)
        ).scalar_one_or_none()
        if existing:
            s.delete(existing)
            s.commit()
    return {"status": "ok"}


# ── KBs ─────────────────────────────────────────────────────────────
@app.get("/kbs")
def list_kbs() -> list[dict]:
    with get_session() as s:
        rows = s.execute(select(KnowledgeBase).order_by(KnowledgeBase.slug)).scalars().all()
        out = []
        for kb in rows:
            n_active = s.execute(
                select(func.count(KbFact.id)).where(KbFact.kb_id == kb.id, KbFact.status == "active")
            ).scalar() or 0
            n_pending = s.execute(
                select(func.count(KbFact.id)).where(KbFact.kb_id == kb.id, KbFact.status == "pending_review")
            ).scalar() or 0
            out.append({
                "slug": kb.slug,
                "name": kb.name,
                "description": kb.description,
                "counts": {"active": n_active, "pending": n_pending},
            })
        return out


@app.post("/kbs")
def create_kb(payload: KbIn) -> dict:
    with get_session() as s:
        existing = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == payload.slug)).scalar_one_or_none()
        if existing:
            raise HTTPException(409, "kb already exists")
        kb = KnowledgeBase(slug=payload.slug, name=payload.name, description=payload.description or "")
        s.add(kb)
        s.commit()
    return {"status": "ok", "slug": payload.slug}


@app.get("/kbs/{slug}")
def get_kb(slug: str) -> dict:
    with get_session() as s:
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == slug)).scalar_one_or_none()
        if not kb:
            raise HTTPException(404, "kb not found")
        facts = s.execute(
            select(KbFact).where(KbFact.kb_id == kb.id).order_by(KbFact.key, KbFact.version.desc())
        ).scalars().all()
        # También: en qué grupos está suscrita
        groups = s.execute(
            select(Group.wa_jid, Group.display_name, GroupKb.priority)
            .join(GroupKb, GroupKb.group_id == Group.id)
            .where(GroupKb.kb_id == kb.id)
        ).all()
        return {
            "slug": kb.slug,
            "name": kb.name,
            "description": kb.description,
            "facts": [
                {
                    "id": f.id,
                    "key": f.key,
                    "value": f.value,
                    "version": f.version,
                    "status": f.status,
                    "source": f.source,
                    "valid_from": f.valid_from.isoformat() if f.valid_from else None,
                    "valid_until": f.valid_until.isoformat() if f.valid_until else None,
                }
                for f in facts
            ],
            "groups": [
                {"wa_jid": j, "display_name": n, "priority": p} for j, n, p in groups
            ],
        }


@app.post("/kbs/{slug}/facts")
def create_fact(slug: str, payload: FactIn) -> dict:
    with get_session() as s:
        kb = s.execute(select(KnowledgeBase).where(KnowledgeBase.slug == slug)).scalar_one_or_none()
        if not kb:
            raise HTTPException(404, "kb not found")
        existing = s.execute(
            select(KbFact)
            .where(KbFact.kb_id == kb.id, KbFact.key == payload.key, KbFact.status == "active")
            .order_by(KbFact.version.desc())
        ).scalar_one_or_none()
        version = (existing.version + 1) if existing else 1
        if existing:
            existing.status = "superseded"
        f = KbFact(kb_id=kb.id, key=payload.key, value=payload.value, source="jmf", version=version, status="active")
        s.add(f)
        s.commit()
        s.refresh(f)
        return {"status": "ok", "id": f.id, "version": version}


@app.post("/facts/{fact_id}/approve")
def approve_fact(fact_id: int) -> dict:
    with get_session() as s:
        f = s.get(KbFact, fact_id)
        if not f:
            raise HTTPException(404, "fact not found")
        if f.status != "pending_review":
            raise HTTPException(400, f"fact not pending_review (status={f.status})")
        prev = s.execute(
            select(KbFact).where(KbFact.kb_id == f.kb_id, KbFact.key == f.key, KbFact.status == "active")
        ).scalar_one_or_none()
        if prev:
            prev.status = "superseded"
        f.status = "active"
        f.valid_from = datetime.utcnow()
        s.commit()
    return {"status": "ok"}


@app.delete("/facts/{fact_id}")
def delete_fact(fact_id: int) -> dict:
    with get_session() as s:
        f = s.get(KbFact, fact_id)
        if not f:
            raise HTTPException(404, "fact not found")
        s.delete(f)
        s.commit()
    return {"status": "ok"}


# ── Audit ───────────────────────────────────────────────────────────
@app.get("/audit")
def list_audit(limit: int = 50, kind: Optional[str] = None) -> list[dict]:
    with get_session() as s:
        q = select(AuditLog).order_by(AuditLog.id.desc()).limit(min(limit, 500))
        if kind:
            q = select(AuditLog).where(AuditLog.kind == kind).order_by(AuditLog.id.desc()).limit(min(limit, 500))
        rows = s.execute(q).scalars().all()
        out = []
        for a in rows:
            try:
                payload = json.loads(a.payload) if a.payload else {}
            except Exception:  # noqa: BLE001
                payload = {"_raw": a.payload}
            out.append({
                "id": a.id,
                "ts": a.ts.isoformat() if a.ts else None,
                "kind": a.kind,
                "group_jid": a.group_jid,
                "payload": payload,
            })
        return out


# ── Soul cache ──────────────────────────────────────────────────────
@app.post("/soul/reload")
def soul_reload(group_jid: Optional[str] = None) -> dict:
    soul_mod.invalidate_cache(group_jid)
    return {"status": "ok", "invalidated": group_jid or "all"}


# ── Discussion launcher (API) ───────────────────────────────────────
@app.get("/settings")
def get_settings() -> dict:
    """Devuelve config efectiva (DB sobrescribe env). API key sólo status, nunca valor."""
    cfg = app_config.all_config()
    return {
        "owner_jid": {
            "value": cfg.get("owner_jid") or top.phoenix_owner_jid or "",
            "source": "db" if cfg.get("owner_jid") else ("env" if top.phoenix_owner_jid else "unset"),
        },
        "proactive_threshold": {
            "value": float(cfg["proactive_threshold"]) if cfg.get("proactive_threshold") else settings.proactive_threshold,
            "source": "db" if cfg.get("proactive_threshold") else "env",
        },
        "proactive_cooldown_min": {
            "value": int(cfg["proactive_cooldown_min"]) if cfg.get("proactive_cooldown_min") else settings.proactive_cooldown_min,
            "source": "db" if cfg.get("proactive_cooldown_min") else "env",
        },
        "default_soul": {
            "value": cfg.get("default_soul") or "",
            "source": "db" if cfg.get("default_soul") else "fallback",
        },
        # Read-only (sólo env / código)
        "anthropic_api_key": {
            "configured": bool(top.anthropic_api_key),
            "source": "env",
        },
        "models": {
            "default": settings.model_default,
            "safety": settings.model_safety,
            "proactive": settings.model_proactive,
            "source": "env",
        },
        "history_window": settings.history_window,
    }


@app.patch("/settings")
def patch_settings(payload: SettingsPatchIn) -> dict:
    changed: list[str] = []
    if payload.owner_jid is not None:
        v = payload.owner_jid.strip()
        if v and not v.endswith("@s.whatsapp.net") and not v.endswith("@g.us"):
            raise HTTPException(400, "owner_jid debe terminar en @s.whatsapp.net (DM) o vacío para desestablecer")
        app_config.set("owner_jid", v or None)
        changed.append("owner_jid")
    if payload.proactive_threshold is not None:
        if not 0.0 <= payload.proactive_threshold <= 1.0:
            raise HTTPException(400, "threshold debe estar en [0,1]")
        app_config.set("proactive_threshold", str(payload.proactive_threshold))
        changed.append("proactive_threshold")
    if payload.proactive_cooldown_min is not None:
        if payload.proactive_cooldown_min < 0:
            raise HTTPException(400, "cooldown debe ser >= 0")
        app_config.set("proactive_cooldown_min", str(payload.proactive_cooldown_min))
        changed.append("proactive_cooldown_min")
    return {"status": "ok", "changed": changed}


@app.put("/settings/default-soul")
def put_default_soul(payload: DefaultSoulIn) -> dict:
    app_config.set("default_soul", payload.soul_md if payload.soul_md.strip() else None)
    return {"status": "ok"}


@app.delete("/settings/default-soul")
def delete_default_soul() -> dict:
    app_config.set("default_soul", None)
    return {"status": "ok"}


@app.post("/group/start-discussion")
def group_start_discussion(payload: DiscussionIn) -> dict:
    return start_discussion_api(payload.group_jid, payload.topic, auto_publish=payload.auto_publish)
