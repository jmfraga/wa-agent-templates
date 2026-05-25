"""KB ingest desde URL — scrape landing → Haiku extrae facts → upsert kb_facts.

Whitelist estricta de dominios marketing/info/blog. BeautifulSoup para limpiar
HTML. Anthropic Haiku para extraer JSON estructurado.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

import anthropic
import httpx
from sqlalchemy import select

from .config import settings
from .db import get_session
from .models import KbFact, KbFactSource

log = logging.getLogger("iris_brain.kb_ingest")

WHITELIST_DOMAINS: set[str] = {
    "info.simacademy.lat",
    "info.emergencias.com.mx",
    "blog.simacademy.lat",
    "blog.emergencias.com.mx",
    "marketing.simacademy.lat",
}

MAX_HTML_BYTES = 1_000_000  # 1 MB
MAX_TEXT_CHARS = 12_000
HTTP_TIMEOUT_S = 15.0

EXTRACT_KEYS = [
    "nombre",
    "descripcion_corta",
    "precio",
    "precio_miembros",
    "duracion",
    "fecha_inicio",
    "fecha_fin",
    "modalidad",
    "instructor",
    "audiencia",
    "requisitos",
    "incluye",
    "registro_url",
    "contacto",
    "sede",
    "idioma",
    "certificacion",
]


class KbIngestError(Exception):
    def __init__(self, code: str, msg: str) -> None:
        super().__init__(msg)
        self.code = code


def is_whitelisted(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in WHITELIST_DOMAINS


def derive_slug(url: str) -> str:
    """info.simacademy.lat/has-magia-con-claude/ → has-magia-con-claude."""
    try:
        path = urlparse(url).path or ""
    except Exception:  # noqa: BLE001
        return "default"
    segs = [s for s in path.split("/") if s]
    if not segs:
        # raíz del dominio → usa el host como slug
        host = (urlparse(url).hostname or "default").lower()
        return host.split(".")[0]
    last = segs[-1]
    # quitar extensiones tipo .html
    last = re.sub(r"\.(html?|php|aspx?)$", "", last, flags=re.I)
    # limpiar
    last = re.sub(r"[^a-zA-Z0-9_-]+", "-", last).strip("-").lower()
    return last or "default"


def _scrape_html(url: str) -> str:
    """GET HTML respetando límites. Lanza KbIngestError."""
    try:
        with httpx.Client(timeout=HTTP_TIMEOUT_S, follow_redirects=True) as c:
            r = c.get(url, headers={"User-Agent": "iris-kb-ingest/1.0"})
    except httpx.TimeoutException as e:
        raise KbIngestError("timeout", f"timeout al scrapear {url}: {e}")
    except httpx.HTTPError as e:
        raise KbIngestError("http_error", f"error HTTP scrapear {url}: {e}")
    if r.status_code != 200:
        raise KbIngestError("http_status", f"HTTP {r.status_code} al scrapear {url}")
    ct = r.headers.get("content-type", "").lower()
    if "text/html" not in ct and "application/xhtml" not in ct:
        raise KbIngestError("bad_content_type", f"content-type no HTML: {ct}")
    if len(r.content) > MAX_HTML_BYTES:
        raise KbIngestError("too_large", f"HTML > {MAX_HTML_BYTES} bytes ({len(r.content)})")
    return r.text


def _html_to_text(html: str) -> str:
    """Extrae texto plano de <main>/<article>/<body> menos script/style/nav/footer."""
    try:
        from bs4 import BeautifulSoup  # noqa: WPS433
    except ImportError as e:
        raise KbIngestError("missing_dep", f"beautifulsoup4 no instalado: {e}")
    soup = BeautifulSoup(html, "html.parser")
    # quitar elementos ruidosos
    for tag in soup(["script", "style", "nav", "footer", "noscript", "svg", "form"]):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    text = root.get_text(separator="\n", strip=True)
    # colapsar líneas vacías
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    text = "\n".join(lines)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS]
    return text


_SYSTEM_PROMPT = (
    "Extraes facts estructurados de landings de cursos médicos para "
    "un knowledge base de un asistente. Devuelve SOLO JSON, sin explicación."
)


def _extract_with_haiku(url: str, text: str) -> dict[str, str]:
    """Llama Haiku, devuelve dict[str,str]. Lanza KbIngestError si JSON inválido."""
    user_msg = (
        f"URL: {url}\n\nTexto:\n{text}\n\n"
        "Extrae como JSON con estas keys (omite las que no encuentres): "
        f"{', '.join(EXTRACT_KEYS)}. "
        "Strings cortos (<200 chars). Sin markdown ni saludos. "
        "Solo objeto JSON plano."
    )
    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except anthropic.APIError as e:
        raise KbIngestError("anthropic_error", f"Anthropic API error: {e}")
    raw = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    cleaned = raw.strip("`")
    if cleaned.lower().startswith("json"):
        cleaned = cleaned[4:].strip()
    # extraer primer objeto JSON si viene envuelto
    match = re.search(r"\{.*\}", cleaned, re.S)
    if match:
        cleaned = match.group(0)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as e:
        raise KbIngestError("bad_json", f"Haiku no devolvió JSON válido: {e}\nRaw: {raw[:500]}")
    if not isinstance(data, dict):
        raise KbIngestError("bad_json", f"Haiku devolvió tipo {type(data).__name__}, esperaba dict")
    # normalizar: keys conocidas, values string corto
    out: dict[str, str] = {}
    for k, v in data.items():
        if not isinstance(k, str) or k not in EXTRACT_KEYS:
            continue
        if v is None or v == "":
            continue
        if isinstance(v, (list, dict)):
            v = json.dumps(v, ensure_ascii=False)
        s = str(v).strip()
        if not s:
            continue
        if len(s) > 500:
            s = s[:500]
        out[k] = s
    return out


def _existing_facts_for_slug(slug: str) -> dict[str, str]:
    with get_session() as s:
        rows = s.scalars(select(KbFact).where(KbFact.kb_slug == slug)).all()
        return {r.key: r.value for r in rows}


def ingest_url(url: str, slug: str | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Pipeline completo. Devuelve dict serializable."""
    if not is_whitelisted(url):
        raise KbIngestError("not_whitelisted", f"Dominio no permitido: {url}")
    final_slug = (slug or "").strip() or derive_slug(url)

    html = _scrape_html(url)
    text = _html_to_text(html)
    if not text:
        raise KbIngestError("empty_text", "no se extrajo texto del HTML")
    facts = _extract_with_haiku(url, text)
    if not facts:
        raise KbIngestError("no_facts", "Haiku no extrajo ningún fact reconocible")

    # comparar con existentes para marcar overrides
    existing = _existing_facts_for_slug(final_slug)
    diff: dict[str, dict[str, str | None]] = {}
    for k, v in facts.items():
        old = existing.get(k)
        diff[k] = {"new": v, "old": old, "changed": (old is not None and old != v)}

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "slug": final_slug,
            "url": url,
            "facts": facts,
            "diff": diff,
            "would_upsert": len(facts),
            "text_chars": len(text),
        }

    upserted_ids: list[int] = []
    with get_session() as s:
        for k, v in facts.items():
            cf = s.scalar(
                select(KbFact).where(KbFact.kb_slug == final_slug, KbFact.key == k)
            )
            if cf is None:
                cf = KbFact(
                    kb_slug=final_slug,
                    key=k,
                    value=v,
                    source=KbFactSource.landing,
                    ttl_days=90,
                    version=1,
                )
                s.add(cf)
            else:
                cf.value = v
                cf.source = KbFactSource.landing
                cf.version = cf.version + 1
            s.flush()
            upserted_ids.append(cf.id)
    return {
        "ok": True,
        "dry_run": False,
        "slug": final_slug,
        "url": url,
        "facts_count": len(facts),
        "facts": facts,
        "diff": diff,
        "upserted_ids": upserted_ids,
    }


def upsert_selected(slug: str, facts: dict[str, str]) -> dict[str, Any]:
    """Upsert manual de un dict ya curado por el usuario (post-preview)."""
    if not slug:
        raise KbIngestError("bad_slug", "slug requerido")
    upserted_ids: list[int] = []
    with get_session() as s:
        for k, v in facts.items():
            if not k or not v:
                continue
            cf = s.scalar(
                select(KbFact).where(KbFact.kb_slug == slug, KbFact.key == k)
            )
            if cf is None:
                cf = KbFact(
                    kb_slug=slug,
                    key=k,
                    value=v,
                    source=KbFactSource.landing,
                    ttl_days=90,
                    version=1,
                )
                s.add(cf)
            else:
                cf.value = v
                cf.source = KbFactSource.landing
                cf.version = cf.version + 1
            s.flush()
            upserted_ids.append(cf.id)
    return {"ok": True, "slug": slug, "facts_count": len(upserted_ids), "upserted_ids": upserted_ids}
