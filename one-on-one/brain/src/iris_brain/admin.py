"""Admin layer para Iris v2 (Sprint 3).

Endpoints `/admin/*` que la UI consume para configurar Iris sin tocar .env
ni reiniciar uvicorn. Auth simple via header X-Iris-Admin-Token.

Responsabilidades:
- Lectura/escritura de runtime_config (key/value).
- Rotación de ANTHROPIC_API_KEY con ping de validación + escritura atómica de .env.
- Lectura/escritura/reload de SOUL.md (con backups).
- Vistas agregadas para tickets, métricas, health.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from . import config as config_mod
from . import soul
from .config import KNOWN_MODELS, OVERRIDABLE_KEYS, settings
from .db import get_session
from .models import (
    Contact,
    Message,
    MessageDirection,
    RuntimeConfig,
    Ticket,
    TicketStatus,
)

log = logging.getLogger("iris_brain.admin")

# ---------------------------------------------------------------------------
# Constantes / precios
# ---------------------------------------------------------------------------

# Precios aproximados USD por MTok (v1, ok para dashboard).
MODEL_PRICES: dict[str, dict[str, float]] = {
    "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
}

# Path a brain/.env (lo resolvemos relativo a este módulo).
_BRAIN_ROOT = Path(__file__).resolve().parents[2]  # .../brain
ENV_PATH = _BRAIN_ROOT / ".env"

ANTHROPIC_KEY_RE = re.compile(r"^sk-ant-api03-[A-Za-z0-9_\-]{20,}$")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def _ensure_admin_token() -> str:
    """Devuelve token activo. Si env vacío, genera uno y lo persiste en DB."""
    if settings.IRIS_ADMIN_TOKEN:
        return settings.IRIS_ADMIN_TOKEN
    try:
        with get_session() as s:
            row = s.get(RuntimeConfig, "admin_token")
            if row is None:
                tok = secrets.token_urlsafe(32)
                s.add(RuntimeConfig(key="admin_token", value=tok, updated_by="bootstrap"))
                log.warning("admin_token generado y persistido en runtime_config (env vacío)")
                return tok
            return row.value
    except Exception:
        # DB no lista todavía: token efímero in-memory.
        if not getattr(_ensure_admin_token, "_fallback", None):
            _ensure_admin_token._fallback = secrets.token_urlsafe(32)  # type: ignore[attr-defined]
            log.warning("admin_token efímero (DB no disponible)")
        return _ensure_admin_token._fallback  # type: ignore[attr-defined]


def check_admin_token(provided: str | None) -> bool:
    """Comparación constante-tiempo."""
    if not provided:
        return False
    expected = _ensure_admin_token()
    return secrets.compare_digest(provided, expected)


# ---------------------------------------------------------------------------
# Runtime config helpers
# ---------------------------------------------------------------------------


def _set_runtime(key: str, value: str, updated_by: str | None = None) -> None:
    with get_session() as s:
        row = s.get(RuntimeConfig, key)
        if row is None:
            s.add(RuntimeConfig(key=key, value=value, updated_by=updated_by))
        else:
            row.value = value
            row.updated_by = updated_by


def _get_runtime(key: str) -> str | None:
    with get_session() as s:
        row = s.get(RuntimeConfig, key)
        return row.value if row else None


def get_config_snapshot() -> dict[str, Any]:
    """Snapshot del estado actual: env + DB overrides combinados."""
    config_mod.load_overrides()
    out: dict[str, Any] = {}
    for db_key in OVERRIDABLE_KEYS:
        out[db_key] = {
            "value": config_mod.get_setting(db_key),
            "source": config_mod.override_source(db_key),
        }

    # API key: nunca devolvemos la real.
    masked_row = _get_runtime("anthropic_api_key_masked")
    if settings.ANTHROPIC_API_KEY:
        key = settings.ANTHROPIC_API_KEY
        masked = _mask_key(key)
        source = "env"
    elif masked_row:
        masked = masked_row
        source = "db"
    else:
        masked = None
        source = None
    out["anthropic_api_key"] = {
        "set": bool(settings.ANTHROPIC_API_KEY or masked_row),
        "masked": masked,
        "source": source,
    }
    out["known_models"] = KNOWN_MODELS
    return out


def update_config(updates: dict[str, Any], updated_by: str | None = None) -> dict[str, Any]:
    """PUT /admin/config. Valida y persiste en runtime_config."""
    errors: list[str] = []
    accepted: dict[str, Any] = {}
    for key, raw in updates.items():
        if key not in OVERRIDABLE_KEYS:
            errors.append(f"key desconocida: {key}")
            continue
        if key in {"model_default", "model_safety"}:
            if raw not in KNOWN_MODELS:
                errors.append(f"{key}={raw!r} no está en KNOWN_MODELS")
                continue
        if key == "max_tokens":
            try:
                ival = int(raw)
            except (TypeError, ValueError):
                errors.append(f"max_tokens debe ser int (recibido {raw!r})")
                continue
            if ival < 1 or ival > 64000:
                errors.append("max_tokens fuera de rango [1, 64000]")
                continue
            raw = str(ival)
        if key == "thinking" and raw not in {"off", "adaptive", "disabled"}:
            errors.append(f"thinking inválido: {raw!r}")
            continue
        if key == "effort" and raw not in {"", "low", "medium", "high", "max"}:
            errors.append(f"effort inválido: {raw!r}")
            continue
        if key == "prompt_caching_enabled" and str(raw).lower() not in {"true", "false"}:
            errors.append(f"prompt_caching_enabled debe ser 'true'/'false' (recibido {raw!r})")
            continue
        _set_runtime(key, str(raw), updated_by=updated_by)
        accepted[key] = str(raw)

    config_mod.load_overrides()
    return {"ok": not errors, "accepted": accepted, "errors": errors}


def reload_config() -> dict[str, Any]:
    config_mod.load_overrides()
    return {"ok": True, "snapshot": get_config_snapshot()}


# ---------------------------------------------------------------------------
# API key rotation
# ---------------------------------------------------------------------------


def _mask_key(key: str) -> str:
    if not key:
        return ""
    tail = key[-6:]
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"sha:{digest}…{tail}"


def _write_env_atomic(new_key: str) -> None:
    """Reemplaza línea ANTHROPIC_API_KEY= preservando el resto del .env.

    Atomic: tmp + rename. chmod 600.
    """
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()

    new_lines: list[str] = []
    replaced = False
    for ln in lines:
        if ln.startswith("ANTHROPIC_API_KEY="):
            new_lines.append(f"ANTHROPIC_API_KEY={new_key}")
            replaced = True
        else:
            new_lines.append(ln)
    if not replaced:
        new_lines.append(f"ANTHROPIC_API_KEY={new_key}")

    tmp = ENV_PATH.with_suffix(ENV_PATH.suffix + ".tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, ENV_PATH)


def _ping_anthropic(api_key: str) -> tuple[bool, str, str | None]:
    """Llama Anthropic con 5 tokens. Devuelve (ok, model_used, error?).

    Usa el modelo default actual.
    """
    import anthropic

    model = settings.IRIS_BRAIN_MODEL_DEFAULT
    try:
        client = anthropic.Anthropic(api_key=api_key)
        client.messages.create(
            model=model,
            max_tokens=5,
            messages=[{"role": "user", "content": "ping"}],
        )
        return True, model, None
    except Exception as e:  # pragma: no cover - paths del API real
        return False, model, str(e)


def rotate_key(new_key: str, updated_by: str | None = None) -> dict[str, Any]:
    """Pipeline completo de rotación. Devuelve dict para el endpoint.

    Lanza ValueError para 400 (formato), RuntimeError para 502 (ping falló).
    """
    if not ANTHROPIC_KEY_RE.match(new_key or ""):
        raise ValueError("formato inválido: se esperaba sk-ant-api03-…")

    ok, model_used, err = _ping_anthropic(new_key)
    if not ok:
        raise RuntimeError(f"ping a Anthropic falló: {err}")

    _write_env_atomic(new_key)
    masked = _mask_key(new_key)
    _set_runtime("anthropic_api_key_masked", masked, updated_by=updated_by)

    # Reset in-memory settings + singletons.
    object.__setattr__(settings, "ANTHROPIC_API_KEY", new_key)
    try:
        from . import chat as chat_mod
        from . import intents as intents_mod

        chat_mod._client = None
        intents_mod._client = None
    except Exception:
        log.exception("no se pudo resetear singletons Anthropic")

    return {
        "ok": True,
        "masked": masked,
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "model_test_used": model_used,
    }


# ---------------------------------------------------------------------------
# SOUL
# ---------------------------------------------------------------------------


def get_soul() -> dict[str, Any]:
    return soul.soul_info()


def put_soul(text: str, updated_by: str | None = None) -> dict[str, Any]:
    if text is None:
        raise ValueError("text es obligatorio")
    info = soul.save(text, updated_by=updated_by)
    return {"ok": True, **info}


def reload_soul() -> dict[str, Any]:
    soul.reload()
    return {"ok": True, **soul.soul_info()}


# ---------------------------------------------------------------------------
# Tickets live
# ---------------------------------------------------------------------------


def _ticket_row(t: Ticket, contact: Contact | None, last_in: Message | None, last_out: Message | None) -> dict[str, Any]:
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
        "contact_name": contact.name if contact else None,
        "contact_phone": contact.phone if contact else None,
        "last_message_in": _msg_brief(last_in),
        "last_message_out": _msg_brief(last_out),
    }


def _msg_brief(m: Message | None) -> dict[str, Any] | None:
    if m is None:
        return None
    return {
        "body": m.body,
        "ts": m.ts.isoformat() if m.ts else None,
    }


def tickets_live(since: datetime | None = None) -> dict[str, Any]:
    """Tickets de últimos 30 días (o ?since=…) agrupados por status."""
    cutoff = since or (datetime.now(timezone.utc) - timedelta(days=30))
    with get_session() as s:
        rows = list(
            s.scalars(
                select(Ticket)
                .where(Ticket.updated_at >= cutoff)
                .order_by(Ticket.updated_at.desc())
            )
        )
        grouped: dict[str, list[dict[str, Any]]] = {st.value: [] for st in TicketStatus}
        from .models import Thread

        for t in rows:
            thread = s.get(Thread, t.thread_id)
            contact = s.get(Contact, thread.contact_id) if thread else None
            last_in = s.scalar(
                select(Message)
                .where(Message.thread_id == t.thread_id, Message.direction == MessageDirection.in_)
                .order_by(Message.ts.desc())
            )
            last_out = s.scalar(
                select(Message)
                .where(Message.thread_id == t.thread_id, Message.direction == MessageDirection.out)
                .order_by(Message.ts.desc())
            )
            grouped[t.status.value].append(_ticket_row(t, contact, last_in, last_out))
        return {
            "since": cutoff.isoformat(),
            "counts": {k: len(v) for k, v in grouped.items()},
            "groups": grouped,
        }


def reassign_ticket_kind(ticket_id: int, new_kind: str) -> dict[str, Any]:
    with get_session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise LookupError("ticket no existe")
        t.kind = new_kind
        t.updated_at = datetime.now(timezone.utc)
        return {"ok": True, "ticket_id": ticket_id, "kind": new_kind}


def close_ticket(ticket_id: int) -> None:
    with get_session() as s:
        t = s.get(Ticket, ticket_id)
        if t is None:
            raise LookupError("ticket no existe")
        t.status = TicketStatus.closed
        t.updated_at = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _model_family(name: str | None) -> str | None:
    if not name:
        return None
    n = name.lower()
    if "haiku" in n:
        return "haiku"
    if "sonnet" in n:
        return "sonnet"
    if "opus" in n:
        return "opus"
    return None


def metrics_range(start: datetime, end: datetime) -> dict[str, Any]:
    with get_session() as s:
        messages_in = s.scalar(
            select(func.count(Message.id)).where(
                Message.direction == MessageDirection.in_,
                Message.ts >= start,
                Message.ts < end,
            )
        ) or 0
        messages_out = s.scalar(
            select(func.count(Message.id)).where(
                Message.direction == MessageDirection.out,
                Message.ts >= start,
                Message.ts < end,
            )
        ) or 0
        tickets_open = s.scalar(
            select(func.count(Ticket.id)).where(Ticket.status != TicketStatus.closed)
        ) or 0
        tickets_closed = s.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.status == TicketStatus.closed,
                Ticket.updated_at >= start,
                Ticket.updated_at < end,
            )
        ) or 0

        # Tokens por familia.
        tok_rows = list(
            s.execute(
                select(
                    Message.model_used,
                    func.sum(Message.tokens_input),
                    func.sum(Message.tokens_output),
                ).where(Message.ts >= start, Message.ts < end).group_by(Message.model_used)
            )
        )

        tokens = {
            "haiku": {"input": 0, "output": 0},
            "sonnet": {"input": 0, "output": 0},
            "opus": {"input": 0, "output": 0},
        }
        cost = 0.0
        for model_used, tin, tout in tok_rows:
            fam = _model_family(model_used)
            if fam and fam in tokens:
                tokens[fam]["input"] += int(tin or 0)
                tokens[fam]["output"] += int(tout or 0)
            # Costo: usamos precios por modelo exacto si conocemos, si no skip.
            price = MODEL_PRICES.get(model_used or "")
            if price:
                cost += (int(tin or 0) / 1_000_000) * price["input"]
                cost += (int(tout or 0) / 1_000_000) * price["output"]

        # Crisis: heurística — tickets de kind 'urgencia' creados en rango.
        crisis_count = s.scalar(
            select(func.count(Ticket.id)).where(
                Ticket.kind.in_(["urgencia", "posible_urgencia"]),
                Ticket.created_at >= start,
                Ticket.created_at < end,
            )
        ) or 0

    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "messages_in": messages_in,
        "messages_out": messages_out,
        "tickets_open": tickets_open,
        "tickets_closed": tickets_closed,
        "tokens_input_haiku": tokens["haiku"]["input"],
        "tokens_output_haiku": tokens["haiku"]["output"],
        "tokens_input_sonnet": tokens["sonnet"]["input"],
        "tokens_output_sonnet": tokens["sonnet"]["output"],
        "tokens_input_opus": tokens["opus"]["input"],
        "tokens_output_opus": tokens["opus"]["output"],
        "crisis_detections": crisis_count,
        "estimated_cost_usd": round(cost, 4),
    }


def metrics_today() -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    m = metrics_range(start, end)
    # Renombrar a las llaves *_today que pide el contrato.
    return {
        "messages_in_today": m["messages_in"],
        "messages_out_today": m["messages_out"],
        "tickets_open": m["tickets_open"],
        "tickets_closed_today": m["tickets_closed"],
        "tokens_input_haiku": m["tokens_input_haiku"],
        "tokens_output_haiku": m["tokens_output_haiku"],
        "tokens_input_sonnet": m["tokens_input_sonnet"],
        "tokens_output_sonnet": m["tokens_output_sonnet"],
        "crisis_detections_today": m["crisis_detections"],
        "estimated_cost_usd_today": m["estimated_cost_usd"],
    }


# ---------------------------------------------------------------------------
# Health expandido
# ---------------------------------------------------------------------------


def health_components() -> dict[str, Any]:
    """Estado de brain (self), relay-bot, wa-listener, postgres."""
    import httpx

    now = datetime.now(timezone.utc).isoformat()
    components: list[dict[str, Any]] = []

    # brain (self)
    components.append({
        "name": "brain",
        "url": f"http://localhost:{settings.IRIS_BRAIN_PORT}/health",
        "ok": True,
        "last_check": now,
    })

    for name, port in [("relay-bot", 8098), ("wa-listener", 8099)]:
        url = f"http://localhost:{port}/health"
        entry: dict[str, Any] = {"name": name, "url": url, "last_check": now}
        try:
            r = httpx.get(url, timeout=2.0)
            entry["ok"] = r.status_code < 500
            if not entry["ok"]:
                entry["error"] = f"status {r.status_code}"
        except Exception as e:
            entry["ok"] = False
            entry["error"] = str(e)
        components.append(entry)

    # Postgres: ping vía SELECT 1.
    pg: dict[str, Any] = {"name": "postgres", "url": settings.IRIS_BRAIN_DB_URL, "last_check": now}
    try:
        from sqlalchemy import text as sa_text

        with get_session() as s:
            s.execute(sa_text("SELECT 1"))
        pg["ok"] = True
    except Exception as e:
        pg["ok"] = False
        pg["error"] = str(e)
    components.append(pg)

    return {"components": components, "ok": all(c["ok"] for c in components)}
