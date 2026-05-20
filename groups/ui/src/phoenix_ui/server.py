import io
import json
import logging
from pathlib import Path
from typing import Optional

import httpx
import qrcode
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from . import clients

_log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

app = FastAPI(title="phoenix-ui", version="0.1.0")


# ── Helpers ─────────────────────────────────────────────────────────
def _brain_get(path: str, params: Optional[dict] = None) -> dict | list:
    with clients.brain() as c:
        r = c.get(path, params=params)
        r.raise_for_status()
        return r.json()


def _brain_call(method: str, path: str, *, json_body: Optional[dict] = None) -> dict:
    with clients.brain() as c:
        r = c.request(method, path, json=json_body)
        if r.status_code >= 400:
            raise HTTPException(r.status_code, r.text)
        return r.json()


def _audit_summary(kind: str, payload: dict) -> str:
    if kind == "crisis_detected":
        cats = ", ".join(payload.get("categories", []) or [])
        sev = payload.get("severity", "?")
        who = payload.get("contact_name") or payload.get("contact_jid") or "?"
        return f"[{sev}] {cats} · de {who}"
    if kind == "proactive_classified":
        should = "→ respond" if payload.get("should_respond") else "→ silent"
        return f"{should} ({payload.get('intent_kind','?')}, conf {payload.get('confidence', 0):.2f}). {payload.get('reasoning','')[:120]}"
    if kind == "proactive_triggered":
        return f"intent={payload.get('intent_kind','?')}. {payload.get('reply_preview','')[:120]}"
    if kind == "crisis_notify_failed":
        return f"error: {payload.get('error','?')}"
    return json.dumps(payload, ensure_ascii=False)[:200]


def _enrich_audit(rows: list[dict]) -> list[dict]:
    out = []
    for a in rows:
        payload = a.get("payload") or {}
        out.append({
            **a,
            "summary": _audit_summary(a["kind"], payload),
            "payload_pretty": json.dumps(payload, indent=2, ensure_ascii=False),
        })
    return out


# ── Vistas ──────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    health = _brain_get("/health")
    groups = _brain_get("/groups")
    audit = _enrich_audit(_brain_get("/audit", {"limit": 15}))
    return templates.TemplateResponse(
        request, "dashboard.html",
        {"health": health, "groups": groups, "audit": audit},
    )


@app.get("/setup", response_class=HTMLResponse)
def setup(request: Request):
    return templates.TemplateResponse(request, "setup.html", {})


@app.get("/groups", response_class=HTMLResponse)
def groups_page(request: Request):
    groups = _brain_get("/groups")
    return templates.TemplateResponse(request, "groups.html", {"groups": groups})


@app.get("/groups/{wa_jid:path}", response_class=HTMLResponse)
def group_detail_page(request: Request, wa_jid: str):
    g = _brain_get(f"/groups/{wa_jid}")
    all_kbs = _brain_get("/kbs")
    # Filtramos KBs ya suscritas para el dropdown de agregar.
    subscribed = {kb["slug"] for kb in g.get("kbs", [])}
    available = [kb for kb in all_kbs if kb["slug"] not in subscribed]
    return templates.TemplateResponse(request, "group_detail.html", {"g": g, "all_kbs": available})


@app.get("/kbs", response_class=HTMLResponse)
def kbs_page(request: Request):
    kbs = _brain_get("/kbs")
    return templates.TemplateResponse(request, "kbs.html", {"kbs": kbs})


@app.get("/kbs/{slug}", response_class=HTMLResponse)
def kb_detail_page(request: Request, slug: str):
    kb = _brain_get(f"/kbs/{slug}")
    return templates.TemplateResponse(request, "kb_detail.html", {"kb": kb})


@app.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, limit: int = 100):
    audit = _enrich_audit(_brain_get("/audit", {"limit": limit}))
    return templates.TemplateResponse(request, "audit.html", {"audit": audit})


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    s = _brain_get("/settings")
    return templates.TemplateResponse(request, "settings.html", {"s": s})


# API proxy: settings
@app.patch("/api/settings")
async def api_settings_patch(
    owner_jid: Optional[str] = Form(None),
    proactive_threshold: Optional[float] = Form(None),
    proactive_cooldown_min: Optional[int] = Form(None),
):
    body: dict = {}
    if owner_jid is not None:
        body["owner_jid"] = owner_jid
    if proactive_threshold is not None:
        body["proactive_threshold"] = proactive_threshold
    if proactive_cooldown_min is not None:
        body["proactive_cooldown_min"] = proactive_cooldown_min
    return _brain_call("PATCH", "/settings", json_body=body)


@app.put("/api/settings/default-soul")
async def api_default_soul_put(soul_md: str = Form("")):
    return _brain_call("PUT", "/settings/default-soul", json_body={"soul_md": soul_md})


@app.delete("/api/settings/default-soul")
async def api_default_soul_delete():
    return _brain_call("DELETE", "/settings/default-soul")


# ── Partials para HTMX ──────────────────────────────────────────────
@app.get("/_partials/wa-state", response_class=HTMLResponse)
def partial_wa_state(request: Request):
    state = None
    my_jid = None
    try:
        with clients.listener() as c:
            r = c.get("/wa/state")
            if r.is_success:
                d = r.json()
                state = d.get("state")
                my_jid = d.get("my_jid")
    except Exception:
        pass
    return templates.TemplateResponse(request, "_partials_wa_state.html", {"state": state, "my_jid": my_jid})


# ── API proxy: WhatsApp pair ────────────────────────────────────────
@app.get("/api/wa/state")
def api_wa_state():
    with clients.listener() as c:
        try:
            r = c.get("/wa/state")
            return JSONResponse(r.json(), status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=503)


@app.post("/api/wa/pair")
def api_wa_pair(reset: int = 0):
    with clients.listener() as c:
        try:
            r = c.post("/wa/pair", params={"reset": reset} if reset else None)
            return JSONResponse(r.json(), status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=503)


@app.post("/api/wa/logout")
def api_wa_logout():
    with clients.listener() as c:
        try:
            r = c.post("/wa/logout")
            return JSONResponse(r.json(), status_code=r.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=503)


@app.get("/api/wa/qr.png")
def api_wa_qr_png():
    """Devuelve el QR vigente como PNG server-side. No depende de CDN del cliente."""
    try:
        with clients.listener() as c:
            r = c.get("/wa/qr")
            if not r.is_success:
                return Response(status_code=503)
            data = r.json()
    except Exception:
        return Response(status_code=503)
    qr_str = data.get("qr")
    if not qr_str:
        return Response(status_code=204)  # no content yet
    img = qrcode.make(qr_str, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"Cache-Control": "no-cache, no-store"},
    )


@app.get("/api/wa/qr-stream")
def api_wa_qr_stream():
    """Proxy SSE del listener. Mantiene la conexión abierta hasta que cliente la cierre."""
    def event_gen():
        client = httpx.Client(base_url=clients.top.phoenix_listener_url, timeout=None)
        try:
            with client.stream("GET", "/wa/qr-stream") as r:
                for line in r.iter_lines():
                    yield (line + "\n").encode("utf-8")
        except Exception as e:  # noqa: BLE001
            yield f"event: error\ndata: {{\"error\": \"{e}\"}}\n\n".encode("utf-8")
        finally:
            client.close()

    return StreamingResponse(event_gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ── API proxy: Groups ───────────────────────────────────────────────
@app.patch("/api/groups/{wa_jid:path}/mode")
async def api_group_mode(wa_jid: str, mode: str = Form(...)):
    return _brain_call("PATCH", f"/groups/{wa_jid}/mode", json_body={"mode": mode})


@app.patch("/api/groups/{wa_jid:path}/name")
async def api_group_name(wa_jid: str, display_name: str = Form(...)):
    return _brain_call("PATCH", f"/groups/{wa_jid}/name", json_body={"display_name": display_name})


@app.delete("/api/groups/{wa_jid:path}")
async def api_group_delete(wa_jid: str):
    return _brain_call("DELETE", f"/groups/{wa_jid}")


@app.put("/api/groups/{wa_jid:path}/soul")
async def api_group_soul(wa_jid: str, soul_md: str = Form(...)):
    return _brain_call("PUT", f"/groups/{wa_jid}/soul", json_body={"soul_md": soul_md})


@app.post("/api/groups/{wa_jid:path}/kbs")
async def api_group_subscribe(wa_jid: str, kb_slug: str = Form(...), priority: int = Form(0)):
    return _brain_call("POST", f"/groups/{wa_jid}/kbs", json_body={"kb_slug": kb_slug, "priority": priority})


@app.delete("/api/groups/{wa_jid:path}/kbs/{kb_slug}")
async def api_group_unsubscribe(wa_jid: str, kb_slug: str):
    return _brain_call("DELETE", f"/groups/{wa_jid}/kbs/{kb_slug}")


# ── API proxy: KBs ──────────────────────────────────────────────────
@app.post("/api/kbs")
async def api_kb_create(slug: str = Form(...), name: str = Form(...), description: str = Form("")):
    return _brain_call("POST", "/kbs", json_body={"slug": slug, "name": name, "description": description})


@app.post("/api/kbs/{slug}/facts")
async def api_kb_fact_create(slug: str, key: str = Form(...), value: str = Form(...)):
    return _brain_call("POST", f"/kbs/{slug}/facts", json_body={"key": key, "value": value})


@app.post("/api/facts/{fact_id}/approve")
async def api_fact_approve(fact_id: int):
    return _brain_call("POST", f"/facts/{fact_id}/approve")


@app.delete("/api/facts/{fact_id}")
async def api_fact_delete(fact_id: int):
    return _brain_call("DELETE", f"/facts/{fact_id}")


# ── Ingest URL / PDF ────────────────────────────────────────────────
@app.post("/api/kbs/{slug}/ingest-url", response_class=HTMLResponse)
async def api_kb_ingest_url(
    request: Request,
    slug: str,
    url: str = Form(...),
    mode: str = Form("pending_review"),
    instructions: str = Form(""),
):
    payload = {"url": url, "mode": mode}
    if instructions.strip():
        payload["instructions"] = instructions.strip()
    try:
        result = _brain_call("POST", f"/kbs/{slug}/ingest-url", json_body=payload)
    except HTTPException as e:
        return HTMLResponse(
            f'<div class="bg-red-50 border border-red-200 text-red-800 p-3 rounded text-sm">❌ Error: {e.detail}</div>',
            status_code=200,
        )
    msg = (
        f'<div class="bg-emerald-50 border border-emerald-200 text-emerald-800 p-3 rounded text-sm">'
        f'✅ {result.get("saved")} facts agregados ({result.get("mode")}) desde "{result.get("title") or url}". '
        f'<a href="/kbs/{slug}" class="underline">Refresca</a> para revisarlos.</div>'
    )
    return HTMLResponse(msg)


@app.post("/api/kbs/{slug}/ingest-pdf", response_class=HTMLResponse)
async def api_kb_ingest_pdf(
    request: Request,
    slug: str,
    file: UploadFile = File(...),
    mode: str = Form("pending_review"),
    instructions: str = Form(""),
):
    files = {"file": (file.filename, await file.read(), file.content_type or "application/pdf")}
    data = {"mode": mode}
    if instructions.strip():
        data["instructions"] = instructions.strip()
    with clients.brain() as c:
        try:
            r = c.post(f"/kbs/{slug}/ingest-pdf", files=files, data=data, timeout=120.0)
        except Exception as e:
            return HTMLResponse(
                f'<div class="bg-red-50 border border-red-200 text-red-800 p-3 rounded text-sm">❌ Brain unreachable: {e}</div>'
            )
    if r.status_code >= 400:
        return HTMLResponse(
            f'<div class="bg-red-50 border border-red-200 text-red-800 p-3 rounded text-sm">❌ {r.text[:300]}</div>'
        )
    result = r.json()
    msg = (
        f'<div class="bg-emerald-50 border border-emerald-200 text-emerald-800 p-3 rounded text-sm">'
        f'✅ {result.get("saved")} facts agregados ({result.get("mode")}) desde {result.get("filename")}. '
        f'<a href="/kbs/{slug}" class="underline">Refresca</a> para revisarlos.</div>'
    )
    return HTMLResponse(msg)
