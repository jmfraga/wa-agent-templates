"""Admin panel routes for the Iris UI.

These routes mount under ``/admin`` and proxy through ``brain_client`` to
the brain's ``/admin/*`` API. The brain endpoints require the
``X-Iris-Admin-Token`` header which is sourced from ``IRIS_ADMIN_TOKEN``.

All page handlers use the shared ``_render`` helper from ``server`` so
HTMX partials work the same way as the rest of the UI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .brain_client import brain_client
from .config import settings

# Valid intents available for ticket reassignment. Mirrors the brain's
# intent enum; keep in sync if the brain adds new ones.
# TODO(Owner): centralize this list — duplicated with brain/intents.py.
VALID_KINDS = [
    "saludo_smalltalk",
    "consulta_cita",
    "info_curso",
    "urgencia_clinica",
    "seguimiento",
    "facturacion",
    "agradecimiento",
    "otro",
]

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/admin", tags=["admin"])


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    partial: str | None = None,
) -> HTMLResponse:
    ctx = {"request": request, "active": context.get("active", ""), **context}
    if _is_htmx(request) and partial:
        return templates.TemplateResponse(request, partial, ctx)
    return templates.TemplateResponse(request, template, ctx)


def _unauthorized(request: Request, payload: dict[str, Any]) -> HTMLResponse | None:
    if payload.get("admin_unauthorized"):
        return templates.TemplateResponse(
            request,
            "admin/unauthorized.html",
            {"request": request, "active": ""},
            status_code=401,
        )
    return None


# --- Dashboard -------------------------------------------------------


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request) -> HTMLResponse:
    raw = await brain_client.admin_metrics_today()
    if (resp := _unauthorized(request, raw)) is not None:
        return resp
    # Remap brain → template keys
    tokens_in = (raw.get("tokens_input_haiku", 0) or 0) + (raw.get("tokens_input_sonnet", 0) or 0)
    tokens_out = (raw.get("tokens_output_haiku", 0) or 0) + (raw.get("tokens_output_sonnet", 0) or 0)
    metrics = {
        **raw,
        "crisis_today": raw.get("crisis_detections_today", 0),
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "tokens_total": tokens_in + tokens_out,
        "cost_usd_today": raw.get("estimated_cost_usd_today", 0.0),
    }
    health = await brain_client.admin_health_components()
    tickets = await brain_client.admin_tickets_live()
    return _render(
        request,
        "admin/dashboard.html",
        {
            "active": "dashboard",
            "metrics": metrics,
            "health": health,
            "tickets_by_status": tickets.get("counts") or tickets.get("by_status") or {},
            "brain_offline": metrics.get("brain_offline", False),
        },
        partial="admin/_partials/dashboard_cards.html",
    )


# --- Settings --------------------------------------------------------


@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(request: Request) -> HTMLResponse:
    cfg = await brain_client.admin_get_config()
    if (resp := _unauthorized(request, cfg)) is not None:
        return resp
    return _render(
        request,
        "admin/settings.html",
        {
            "active": "settings",
            "cfg": cfg,
            "brain_offline": cfg.get("brain_offline", False),
        },
    )


@router.put("/settings", response_class=HTMLResponse)
@router.post("/settings", response_class=HTMLResponse)
async def admin_settings_save(
    request: Request,
    model_default: str = Form(...),
    model_safety: str = Form(...),
    max_tokens: int = Form(1024),
    thinking: str = Form("off"),
    effort: str = Form("-"),
    prompt_caching_enabled: str = Form(""),
) -> HTMLResponse:
    payload = {
        "model_default": model_default,
        "model_safety": model_safety,
        "max_tokens": max_tokens,
        "thinking": thinking,
        "effort": effort,
        "prompt_caching_enabled": prompt_caching_enabled in ("on", "true", "1"),
    }
    result = await brain_client.admin_update_config(payload)
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    cfg = await brain_client.admin_get_config()
    ok = bool(result.get("ok", not result.get("error")))
    return _render(
        request,
        "admin/settings.html",
        {
            "active": "settings",
            "cfg": cfg,
            "toast": {
                "ok": ok,
                "message": "Configuracion guardada" if ok else (result.get("error") or "Error al guardar"),
            },
            "brain_offline": cfg.get("brain_offline", False),
        },
        partial="admin/_partials/settings_form.html",
    )


@router.post("/rotate-key", response_class=HTMLResponse)
async def admin_rotate_key(request: Request, api_key: str = Form(...)) -> HTMLResponse:
    result = await brain_client.admin_rotate_key(api_key.strip())
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    return templates.TemplateResponse(
        request,
        "admin/_partials/rotate_key_result.html",
        {"request": request, "result": result},
    )


@router.post("/regenerate-token", response_class=HTMLResponse)
async def admin_regenerate_token(request: Request) -> HTMLResponse:
    result = await brain_client.admin_regenerate_token()
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    return templates.TemplateResponse(
        request,
        "admin/_partials/regen_token_result.html",
        {"request": request, "result": result},
    )


# --- SOUL ------------------------------------------------------------


@router.get("/soul", response_class=HTMLResponse)
async def admin_soul(request: Request) -> HTMLResponse:
    soul = await brain_client.admin_get_soul()
    if (resp := _unauthorized(request, soul)) is not None:
        return resp
    return _render(
        request,
        "admin/soul.html",
        {
            "active": "soul",
            "soul": soul,
            "brain_offline": soul.get("brain_offline", False),
        },
    )


@router.post("/soul", response_class=HTMLResponse)
@router.put("/soul", response_class=HTMLResponse)
async def admin_soul_save(request: Request, text: str = Form(...)) -> HTMLResponse:
    result = await brain_client.admin_put_soul(text)
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    soul = await brain_client.admin_get_soul()
    ok = bool(result.get("ok", not result.get("error")))
    return _render(
        request,
        "admin/soul.html",
        {
            "active": "soul",
            "soul": soul,
            "toast": {
                "ok": ok,
                "message": "SOUL guardado, backup creado" if ok else (result.get("error") or "Error al guardar"),
            },
            "brain_offline": soul.get("brain_offline", False),
        },
    )


@router.post("/soul/reload", response_class=HTMLResponse)
async def admin_soul_reload(request: Request) -> HTMLResponse:
    result = await brain_client.admin_reload_soul()
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    soul = await brain_client.admin_get_soul()
    ok = bool(result.get("ok"))
    return _render(
        request,
        "admin/soul.html",
        {
            "active": "soul",
            "soul": soul,
            "toast": {
                "ok": ok,
                "message": "SOUL recargado desde disco" if ok else "No se pudo recargar",
            },
            "brain_offline": soul.get("brain_offline", False),
        },
    )


@router.get("/soul/backup/{ts}", response_class=HTMLResponse)
async def admin_soul_backup(request: Request, ts: str) -> HTMLResponse:
    backup = await brain_client.admin_get_soul_backup(ts)
    if (resp := _unauthorized(request, backup)) is not None:
        return resp
    soul = await brain_client.admin_get_soul()
    return templates.TemplateResponse(
        request,
        "admin/_partials/soul_diff.html",
        {
            "request": request,
            "ts": ts,
            "backup_text": backup.get("text", ""),
            "current_text": soul.get("text", ""),
        },
    )


# --- Tickets ---------------------------------------------------------


@router.get("/tickets", response_class=HTMLResponse)
async def admin_tickets(request: Request, since: str | None = None) -> HTMLResponse:
    data = await brain_client.admin_tickets_live(since=since)
    if (resp := _unauthorized(request, data)) is not None:
        return resp
    statuses = ["open", "awaiting_jmf", "awaiting_patient", "closed"]
    groups = data.get("groups") or {}
    columns = {s: groups.get(s, []) for s in statuses}
    counts = data.get("counts") or {s: len(columns[s]) for s in statuses}
    return _render(
        request,
        "admin/tickets.html",
        {
            "active": "tickets",
            "columns": columns,
            "statuses": statuses,
            "by_status": counts,
            "valid_kinds": VALID_KINDS,
            "brain_offline": data.get("brain_offline", False),
        },
        partial="admin/_partials/tickets_kanban.html",
    )


@router.get("/tickets/{ticket_id}", response_class=HTMLResponse)
async def admin_ticket_drawer(request: Request, ticket_id: str) -> HTMLResponse:
    # Hit brain ticket endpoint (non-admin route already exists for read).
    data = await brain_client._get(f"/tickets/{ticket_id}")  # noqa: SLF001
    return templates.TemplateResponse(
        request,
        "admin/_partials/ticket_drawer.html",
        {
            "request": request,
            "ticket": data,
            "ticket_id": ticket_id,
            "valid_kinds": VALID_KINDS,
        },
    )


@router.post("/tickets/{ticket_id}/reply", response_class=HTMLResponse)
async def admin_ticket_reply(
    request: Request,
    ticket_id: str,
    body: str = Form(...),
    close_after: str = Form(""),
) -> HTMLResponse:
    result = await brain_client.admin_ticket_reply(
        ticket_id,
        body=body,
        close_after=close_after in ("on", "true", "1"),
    )
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    return templates.TemplateResponse(
        request,
        "admin/_partials/reply_result.html",
        {"request": request, "result": result, "ticket_id": ticket_id},
    )


@router.post("/tickets/{ticket_id}/status", response_class=HTMLResponse)
async def admin_ticket_set_status(
    request: Request,
    ticket_id: str,
    status: str = Form(...),
) -> HTMLResponse:
    result = await brain_client.admin_ticket_set_status(ticket_id, status)
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    return templates.TemplateResponse(
        request,
        "admin/_partials/reply_result.html",
        {"request": request, "result": {"ok": True, "moved_to": status, **result}, "ticket_id": ticket_id},
    )


@router.post("/tickets/{ticket_id}/close", response_class=HTMLResponse)
async def admin_ticket_close(request: Request, ticket_id: str) -> HTMLResponse:
    result = await brain_client.admin_ticket_close(ticket_id)
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    return templates.TemplateResponse(
        request,
        "admin/_partials/reply_result.html",
        {"request": request, "result": {"ok": True, "closed": True, **result}, "ticket_id": ticket_id},
    )


@router.post("/tickets/{ticket_id}/reassign-kind", response_class=HTMLResponse)
async def admin_ticket_reassign(
    request: Request,
    ticket_id: str,
    kind: str = Form(...),
) -> HTMLResponse:
    result = await brain_client.admin_ticket_reassign(ticket_id, kind)
    if (resp := _unauthorized(request, result)) is not None:
        return resp
    return templates.TemplateResponse(
        request,
        "admin/_partials/reply_result.html",
        {"request": request, "result": result, "ticket_id": ticket_id},
    )


# --- Metrics ---------------------------------------------------------


@router.get("/metrics", response_class=HTMLResponse)
async def admin_metrics(request: Request) -> HTMLResponse:
    metrics = await brain_client.admin_metrics_today()
    if (resp := _unauthorized(request, metrics)) is not None:
        return resp
    return _render(
        request,
        "admin/metrics.html",
        {
            "active": "metrics",
            "metrics": metrics,
            "brain_offline": metrics.get("brain_offline", False),
        },
    )


# --- Health ----------------------------------------------------------


@router.get("/health", response_class=HTMLResponse)
async def admin_health(request: Request) -> HTMLResponse:
    data = await brain_client.admin_health_components()
    if (resp := _unauthorized(request, data)) is not None:
        return resp
    return _render(
        request,
        "admin/health.html",
        {
            "active": "health",
            "components": data.get("components", []),
            "brain_offline": data.get("brain_offline", False),
        },
        partial="admin/_partials/health_table.html",
    )


@router.get("/tasks", response_class=HTMLResponse)
async def admin_tasks(request: Request, status: str | None = None) -> HTMLResponse:
    import httpx
    params = {}
    if status:
        params["status"] = status
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get(f"{settings.BRAIN_URL}/tasks", params=params)
            data = r.json()
        except httpx.HTTPError:
            data = {"tasks": [], "brain_offline": True}
    return _render(request, "admin/tasks.html", {
        "active": "tasks",
        "tasks": data.get("tasks", []),
        "status": status or "",
        "brain_offline": data.get("brain_offline", False),
    })


@router.get("/tasks/new", response_class=HTMLResponse)
async def admin_task_new(request: Request) -> HTMLResponse:
    # Carga lista de media assets para selector (no eliminados, top 100 recientes).
    import httpx
    assets: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get(f"{settings.BRAIN_URL}/media", params={"limit": 100})
            r.raise_for_status()
            assets = r.json().get("items", [])
        except httpx.HTTPError:
            assets = []
    return _render(request, "admin/task_new.html", {"active": "task_new", "assets": assets})


@router.post("/tasks/{task_id}/execute", response_class=HTMLResponse)
async def admin_task_execute(request: Request, task_id: int) -> HTMLResponse:
    """Botón 'Ejecutar ahora' del detalle de task — proxy a brain."""
    import httpx
    from fastapi.responses import HTMLResponse as _HTML
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            r = await c.post(f"{settings.BRAIN_URL}/tasks/{task_id}/execute")
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            err = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    err = e.response.json().get("detail", err)
                except Exception:
                    pass
            return _HTML(f'<div class="p-3 text-rose-700">✗ Error ejecutando task: {err}</div>')
    sent = data.get("sent", 0)
    failed = data.get("failed", 0)
    status = data.get("status", "?")
    errors = data.get("errors", []) or []
    color = "emerald" if failed == 0 else ("amber" if sent > 0 else "rose")
    err_html = ""
    if errors:
        err_html = "<ul class='mt-2 text-xs list-disc list-inside'>" + "".join(
            f"<li>target {e.get('target_id')}: {e.get('error')}</li>" for e in errors[:10]
        ) + "</ul>"
    return _HTML(
        f'<div class="p-3 text-{color}-700 bg-{color}-50 border border-{color}-200 rounded-md">'
        f'🚀 Task #{task_id} ejecutada — sent={sent} failed={failed} status={status}{err_html} '
        f'<a href="/admin/tasks" class="underline ml-2">refrescar lista</a></div>'
    )


@router.get("/contacts/search", response_class=HTMLResponse)
async def admin_contact_search(request: Request, search: str = "") -> HTMLResponse:
    """HTMX autocomplete: devuelve fragmento con botones para agregar contactos."""
    q = (search or "").strip()
    if len(q) < 2:
        return templates.TemplateResponse(
            request,
            "admin/_partials/contact_search.html",
            {"request": request, "items": []},
        )
    data = await brain_client.list_contacts(q=q, page=1, page_size=10, sort="recent")
    return templates.TemplateResponse(
        request,
        "admin/_partials/contact_search.html",
        {"request": request, "items": data.get("items", [])[:10]},
    )


@router.put("/tickets/{ticket_id}/edit", response_class=HTMLResponse)
async def admin_ticket_edit(
    request: Request,
    ticket_id: int,
    kind: str = Form(""),
    summary: str = Form(""),
    draft_for_owner: str = Form(""),
) -> HTMLResponse:
    from fastapi.responses import HTMLResponse as _HTML
    import httpx
    payload = {}
    if kind:
        payload["kind"] = kind
    if summary:
        payload["summary"] = summary
    if draft_for_owner:
        payload["draft_for_owner"] = draft_for_owner
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.put(f"{settings.BRAIN_URL}/tickets/{ticket_id}", json=payload)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return _HTML(f'<div class="p-2 text-rose-700">✗ {e}</div>')
    return _HTML(f'<div class="p-2 text-emerald-700">✓ Ticket #{ticket_id} actualizado</div>')


@router.delete("/tickets/{ticket_id}/delete", response_class=HTMLResponse)
async def admin_ticket_delete(request: Request, ticket_id: int) -> HTMLResponse:
    from fastapi.responses import HTMLResponse as _HTML
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.delete(f"{settings.BRAIN_URL}/tickets/{ticket_id}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            return _HTML(f'<div class="p-2 text-rose-700">✗ {e}</div>')
    return _HTML(
        f'<div class="p-2 text-emerald-700">🗑 Ticket #{ticket_id} eliminado — '
        f'<a href="/admin/tickets" class="underline">refrescar</a></div>'
    )


@router.post("/tasks/new/preview", response_class=HTMLResponse)
async def admin_task_preview(
    request: Request,
    kind: str = Form(...),
    summary: str = Form(...),
    message_template: str = Form(""),
    target_contact_ids: list[int] = Form(...),
    expected_names: list[str] = Form(...),
    asset_id: str = Form(""),
    caption: str = Form(""),
    scheduled_at: str = Form(""),
) -> HTMLResponse:
    import httpx
    # Si hay asset, el body es el caption; el template puede quedar vacío.
    effective_template = caption if asset_id else message_template
    payload = {
        "kind": kind,
        "summary": summary,
        "raw_instruction": f"[UI admin] {summary}",
        "target_contact_ids": target_contact_ids,
        "expected_names": expected_names,
        "message_template": effective_template or "",
    }
    # Context payload con asset/caption/template y scheduled
    ctx: dict[str, Any] = {}
    if asset_id:
        try:
            ctx["asset_id"] = int(asset_id)
        except ValueError:
            pass
        if caption:
            ctx["caption"] = caption
    if message_template:
        ctx["message_template"] = message_template
    if ctx:
        payload["context"] = ctx
    async with httpx.AsyncClient(timeout=15) as c:
        try:
            r = await c.post(f"{settings.BRAIN_URL}/tasks/preview", json=payload)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            err = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    err = e.response.json().get("detail", err)
                except Exception:
                    pass
            return templates.TemplateResponse(
                request,
                "admin/_partials/task_preview.html",
                {"request": request, "error": err},
            )
        # Si scheduled_at presente o asset_id, persistir extras en la task creada
        task_id = data.get("task_id")
        if task_id and (scheduled_at or asset_id):
            try:
                patch_payload: dict[str, Any] = {}
                if scheduled_at:
                    patch_payload["scheduled_at"] = scheduled_at
                if asset_id:
                    patch_payload["asset_id"] = int(asset_id)
                    patch_payload["caption"] = caption or None
                await c.post(
                    f"{settings.BRAIN_URL}/tasks/{task_id}/patch", json=patch_payload
                )
            except httpx.HTTPError:
                pass
    data["has_asset"] = bool(asset_id)
    data["scheduled_at"] = scheduled_at or None
    return templates.TemplateResponse(
        request,
        "admin/_partials/task_preview.html",
        {"request": request, "preview": data},
    )


@router.post("/tasks/{task_id}/confirm-send", response_class=HTMLResponse)
async def admin_task_send(
    request: Request,
    task_id: int,
    message_template: str = Form(""),
    has_asset: str = Form(""),
) -> HTMLResponse:
    import httpx
    is_asset = has_asset in ("on", "true", "1", "yes")
    async with httpx.AsyncClient(timeout=60) as c:
        try:
            if is_asset:
                # Usar el executor (consulta context.asset_id + caption)
                r = await c.post(f"{settings.BRAIN_URL}/tasks/{task_id}/execute")
            else:
                r = await c.post(
                    f"{settings.BRAIN_URL}/tasks/{task_id}/send-all",
                    json={"message_template": message_template},
                )
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPError as e:
            return templates.TemplateResponse(
                request,
                "admin/_partials/task_preview.html",
                {"request": request, "error": str(e)},
            )
    # Normalizar shape de executor (sent/failed/errors) → results[]
    if is_asset and "results" not in data:
        results = []
        sent = data.get("sent", 0)
        for i in range(sent):
            results.append({"ok": True, "contact_name": None})
        for e in data.get("errors", []) or []:
            results.append({"ok": False, "contact_name": f"target #{e.get('target_id')}", "error": e.get("error")})
        data["results"] = results
        data["ok"] = data.get("failed", 0) == 0
    return templates.TemplateResponse(
        request,
        "admin/_partials/task_preview.html",
        {"request": request, "result": data},
    )


@router.post("/tasks/{task_id}/cancel", response_class=HTMLResponse)
async def admin_cancel_task(request: Request, task_id: int) -> HTMLResponse:
    import httpx
    from fastapi.responses import HTMLResponse as _HTML
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.post(f"{settings.BRAIN_URL}/tasks/{task_id}/cancel")
            r.raise_for_status()
        except httpx.HTTPError as e:
            return _HTML(f'<div class="p-3 text-rose-700">Error al cancelar: {e}</div>')
    return _HTML(f'<div class="p-3 text-emerald-700">✅ Task #{task_id} cancelada — <a href="/admin/tasks" class="underline">refrescar lista</a></div>')


# --- Media (Phase 1c) ----------------------------------------------------


@router.get("/media", response_class=HTMLResponse)
async def admin_media(
    request: Request,
    q: str = "",
    source: str = "",
) -> HTMLResponse:
    import httpx
    params: dict[str, str] = {}
    if q:
        params["q"] = q
    if source:
        params["source"] = source
    items: list[dict] = []
    brain_offline = False
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.get(f"{settings.BRAIN_URL}/media", params=params)
            r.raise_for_status()
            data = r.json()
            items = data.get("items", [])
        except httpx.HTTPError:
            brain_offline = True
    return _render(
        request,
        "admin/media.html",
        {
            "active": "media",
            "items": items,
            "q": q,
            "source": source,
            "brain_offline": brain_offline,
        },
        partial="admin/_partials/media_grid.html",
    )


@router.post("/media/upload", response_class=HTMLResponse)
async def admin_media_upload(
    request: Request,
    file: UploadFile = File(...),
    label: str = Form(""),
    tags: str = Form(""),
) -> HTMLResponse:
    import httpx
    from fastapi.responses import HTMLResponse as _HTML
    data = await file.read()
    files = {"file": (file.filename or "upload.bin", data, file.content_type or "application/octet-stream")}
    form_data = {"source": "ui_upload"}
    if label:
        form_data["label"] = label
    if tags:
        form_data["tags"] = tags
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post(f"{settings.BRAIN_URL}/media/upload", files=files, data=form_data)
            r.raise_for_status()
            res = r.json()
        except httpx.HTTPError as e:
            err = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    err = e.response.json().get("detail", err)
                except Exception:
                    pass
            return _HTML(f'<div class="p-3 text-rose-700">✗ {err}</div>')
    dedupe = " (dedupe)" if res.get("dedupe") else ""
    return _HTML(
        f'<div class="p-3 text-emerald-700">✓ Subida \'{res.get("label") or res.get("filename")}\' id={res["id"]}{dedupe} — '
        f'<a href="/admin/media" class="underline">refrescar</a></div>'
    )


@router.post("/media/ingest-url", response_class=HTMLResponse)
async def admin_media_ingest_url(
    request: Request,
    url: str = Form(...),
    label: str = Form(""),
    tags: str = Form(""),
) -> HTMLResponse:
    import httpx
    from fastapi.responses import HTMLResponse as _HTML
    payload: dict = {"url": url, "source": "marketing"}
    if label:
        payload["label"] = label
    if tags:
        payload["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    async with httpx.AsyncClient(timeout=30) as c:
        try:
            r = await c.post(f"{settings.BRAIN_URL}/media/ingest-url", json=payload)
            r.raise_for_status()
            res = r.json()
        except httpx.HTTPError as e:
            err = str(e)
            if hasattr(e, "response") and e.response is not None:
                try:
                    err = e.response.json().get("detail", err)
                except Exception:
                    pass
            return _HTML(f'<div class="p-3 text-rose-700">✗ {err}</div>')
    dedupe = " (dedupe)" if res.get("dedupe") else ""
    return _HTML(
        f'<div class="p-3 text-emerald-700">✓ Importada \'{res.get("label") or res.get("filename")}\' id={res["id"]}{dedupe} — '
        f'<a href="/admin/media" class="underline">refrescar</a></div>'
    )


@router.delete("/media/{asset_id}", response_class=HTMLResponse)
async def admin_media_delete(request: Request, asset_id: int) -> HTMLResponse:
    import httpx
    from fastapi.responses import HTMLResponse as _HTML
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.delete(f"{settings.BRAIN_URL}/media/{asset_id}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            return _HTML(f'<div class="p-2 text-rose-700">✗ {e}</div>')
    return _HTML(
        f'<div class="p-2 text-emerald-700">🗑 Media #{asset_id} borrada — '
        f'<a href="/admin/media" class="underline">refrescar</a></div>'
    )


@router.get("/whatsapp", response_class=HTMLResponse)
async def admin_whatsapp(request: Request) -> HTMLResponse:
    return _render(request, "admin/whatsapp.html", {"active": "whatsapp"})


@router.get("/whatsapp/status", response_class=HTMLResponse)
async def admin_whatsapp_status(request: Request) -> HTMLResponse:
    """Card de status — polling cada 10s vía HTMX."""
    import httpx
    from fastapi.responses import HTMLResponse as _HTML

    state = {"connected": False, "error": None}
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get("http://localhost:8099/health")
            d = r.json()
            state["connected"] = bool(d.get("connected"))
            state["port"] = d.get("port")
    except Exception as e:  # noqa: BLE001
        state["error"] = str(e)

    if state["connected"]:
        html = (
            '<div class="bg-emerald-50 border border-emerald-200 rounded-lg shadow-sm p-6">'
            '<div class="flex items-center gap-3">'
            '<span class="inline-block w-3 h-3 rounded-full bg-emerald-500 animate-pulse"></span>'
            '<div><div class="font-semibold text-emerald-900">Iris conectada a WhatsApp</div>'
            '<div class="text-sm text-emerald-700">Sesión Baileys activa en puerto '
            + str(state.get("port", "?")) + ' (RPi5). Atendiendo mensajes.</div></div></div></div>'
        )
    elif state["error"]:
        html = (
            '<div class="bg-rose-50 border border-rose-200 rounded-lg shadow-sm p-6">'
            '<div class="flex items-center gap-3">'
            '<span class="inline-block w-3 h-3 rounded-full bg-rose-500"></span>'
            '<div><div class="font-semibold text-rose-900">wa-listener no responde</div>'
            f'<div class="text-sm text-rose-700">{state["error"]}</div></div></div></div>'
        )
    else:
        html = (
            '<div class="bg-amber-50 border border-amber-200 rounded-lg shadow-sm p-6">'
            '<div class="flex items-center gap-3">'
            '<span class="inline-block w-3 h-3 rounded-full bg-amber-500 animate-pulse"></span>'
            '<div><div class="font-semibold text-amber-900">Sesión Baileys caída</div>'
            '<div class="text-sm text-amber-700">El listener corre pero no está vinculado a WhatsApp. Escanea el QR abajo.</div></div></div></div>'
        )
    return _HTML(html)


@router.post("/health/probe/{name}", response_class=HTMLResponse)
async def admin_health_probe(request: Request, name: str) -> HTMLResponse:
    # TODO(Owner): the brain currently has no manual-probe endpoint; this
    # just re-fetches the components list. Wire a dedicated probe route
    # under /admin/health/probe/{name} when available.
    data = await brain_client.admin_health_components()
    if (resp := _unauthorized(request, data)) is not None:
        return resp
    return templates.TemplateResponse(
        request,
        "admin/_partials/health_table.html",
        {"request": request, "components": data.get("components", [])},
    )
