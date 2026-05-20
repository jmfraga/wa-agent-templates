"""FastAPI server for the Iris v2 UI.

All routes are GET-only except the kb-facts upsert which accepts a
POST from an HTMX form. HTMX requests (identified by the ``HX-Request``
header) get partial fragments; full page loads get the wrapping
``base.html`` layout.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .admin_routes import router as admin_router
from .brain_client import brain_client
from .config import settings

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

# The package ships its own templates dir but we also want a project-level
# /static at <project-root>/ui/static for assets that don't
# live inside the package.
PROJECT_STATIC_DIR = BASE_DIR.parent.parent.parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="Iris UI", version="0.1.0")

# Prefer project-level static dir if it exists, otherwise the package dir.
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Admin panel (/admin/*) — settings, SOUL, tickets live, metrics, health.
app.include_router(admin_router)


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def _render(
    request: Request,
    template: str,
    context: dict[str, Any],
    *,
    partial: str | None = None,
) -> HTMLResponse:
    """Render full page or HTMX partial depending on the request."""
    ctx = {"request": request, **context}
    if _is_htmx(request) and partial:
        return templates.TemplateResponse(request, partial, ctx)
    return templates.TemplateResponse(request, template, ctx)


# --- Routes ----------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
async def directory(
    request: Request,
    q: str | None = None,
    kind: str | None = None,
    page: int = 1,
    sort: str = "name",
) -> HTMLResponse:
    data = await brain_client.list_contacts(q=q, kind=kind, page=page, sort=sort)
    contacts = data.get("items", [])
    return _render(
        request,
        "directory.html",
        {
            "contacts": contacts,
            "q": q or "",
            "kind": kind or "",
            "sort": sort,
            "page": page,
            "brain_offline": data.get("brain_offline", False),
            "total": data.get("total"),
        },
        partial="_partials/contacts_table.html",
    )


@app.delete("/contact/{phone}", response_class=HTMLResponse)
async def contact_delete(request: Request, phone: str) -> HTMLResponse:
    from fastapi.responses import HTMLResponse
    import httpx
    async with httpx.AsyncClient(timeout=10) as c:
        try:
            r = await c.delete(f"{settings.BRAIN_URL}/contacts/{phone}")
            r.raise_for_status()
        except httpx.HTTPError as e:
            return HTMLResponse(f'<span class="text-rose-600">✗ {e}</span>')
    return HTMLResponse('<span class="text-emerald-600">✓ eliminado — <a href="/" class="underline">volver al directorio</a></span>')


@app.put("/contact/{phone}", response_class=HTMLResponse)
async def contact_update(
    request: Request,
    phone: str,
    name: str = Form(""),
    kind: str = Form("otro"),
    notes: str = Form(""),
) -> HTMLResponse:
    from fastapi.responses import HTMLResponse

    result = await brain_client._put(f"/contacts/{phone}", {"name": name, "kind": kind, "notes": notes})
    if result.get("ok"):
        return HTMLResponse('<span class="text-emerald-600">✓ guardado</span>')
    return HTMLResponse(f'<span class="text-rose-600">✗ {result.get("error", "error")}</span>')


@app.get("/contact/{phone}", response_class=HTMLResponse)
async def contact_detail(request: Request, phone: str) -> HTMLResponse:
    data = await brain_client.get_contact(phone)
    return _render(
        request,
        "contact.html",
        {
            "phone": phone,
            "contact": data.get("contact") or data,
            "thread": data.get("thread") or {},
            "messages": data.get("messages") or [],
            "tickets": data.get("tickets") or [],
            "brain_offline": data.get("brain_offline", False),
        },
    )


@app.get("/tickets", response_class=HTMLResponse)
async def tickets(request: Request) -> HTMLResponse:
    """Redirect a la vista admin de tickets (la fuente de verdad)."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/admin/tickets", status_code=302)


def _group_facts(items: list[dict]) -> dict[str, list[dict]]:
    """Agrupa kb_facts por slug, mantiene orden (slug, key)."""
    groups: dict[str, list[dict]] = {}
    for f in items:
        slug = f.get("slug") or f.get("kb_slug") or "(sin slug)"
        groups.setdefault(slug, []).append(f)
    # Pone '_global' al final (info catch-all)
    if "_global" in groups:
        g = groups.pop("_global")
        groups["_global"] = g
    return groups


@app.get("/courses", response_class=HTMLResponse)
async def courses(request: Request) -> HTMLResponse:
    data = await brain_client.list_kb_facts()
    return _render(
        request,
        "courses.html",
        {
            "facts": data.get("items", []),
            "groups": _group_facts(data.get("items", [])),
            "brain_offline": data.get("brain_offline", False),
        },
    )


@app.post("/courses", response_class=HTMLResponse)
async def upsert_kb(
    request: Request,
    slug: str = Form(...),
    key: str = Form(...),
    value: str = Form(...),
) -> HTMLResponse:
    await brain_client.upsert_kb_fact(slug=slug, key=key, value=value)
    data = await brain_client.list_kb_facts()
    return _render(
        request,
        "courses.html",
        {
            "facts": data.get("items", []),
            "groups": _group_facts(data.get("items", [])),
            "brain_offline": data.get("brain_offline", False),
        },
    )


@app.get("/health")
async def health() -> JSONResponse:
    brain_health = await brain_client.get_health()
    return JSONResponse(
        {
            "ui": {
                "status": "ok",
                "port": settings.IRIS_UI_PORT,
            },
            "brain": brain_health,
        }
    )
