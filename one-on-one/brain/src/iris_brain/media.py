"""Iris media (Phase 1c) — ingest, dedupe, search.

3 fuentes alimentan un storage unificado:
  - 'marketing'  → URL whitelisted (marketing.example.com, etc.)
  - 'ui_upload'  → drag-drop en /admin/media
  - 'telegram'   → owner manda foto al bot con caption 'guarda como X'
  - 'whatsapp'   → owner manda foto al número WA propio (owner) con caption ingest

Dedupe por sha256. Soft-delete vía deleted_at (preserva FK histórico en messages).

Whitelist de mime + 10 MB max + dominios.
"""
from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import or_, select

from .config import settings
from .db import get_session
from .models import MediaAsset, MediaSource

log = logging.getLogger("iris_brain.media")

ALLOWED_MIMES = {"image/jpeg", "image/png", "image/webp", "application/pdf"}
EXT_BY_MIME = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


class MediaError(Exception):
    """Errores de validación de media. Tienen un `code` para mapear a HTTP."""

    def __init__(self, code: str, msg: str) -> None:
        super().__init__(msg)
        self.code = code


def _ensure_storage_dir() -> Path:
    p = Path(settings.MEDIA_STORAGE_DIR)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _is_whitelisted_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return False
    return host in {d.lower() for d in settings.MEDIA_WHITELIST_DOMAINS}


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _validate_mime(mime: str) -> None:
    if mime not in ALLOWED_MIMES:
        raise MediaError("bad_mime", f"mime_type no permitido: {mime}")


def _validate_size(n: int) -> None:
    if n > settings.MEDIA_MAX_BYTES:
        raise MediaError("too_large", f"archivo de {n} bytes > max {settings.MEDIA_MAX_BYTES}")
    if n <= 0:
        raise MediaError("empty", "archivo vacío")


def _to_dict(m: MediaAsset) -> dict[str, Any]:
    return {
        "id": m.id,
        "source": m.source.value if m.source else None,
        "filename": m.filename,
        "mime_type": m.mime_type,
        "size_bytes": m.size_bytes,
        "sha256": m.sha256,
        "origin_url": m.origin_url,
        "label": m.label,
        "tags": m.tags or [],
        "use_count": m.use_count,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "last_used_at": m.last_used_at.isoformat() if m.last_used_at else None,
        "deleted_at": m.deleted_at.isoformat() if m.deleted_at else None,
        "preview_url": f"/media/{m.id}/raw",
    }


def list_for_contact(phone: str) -> dict[str, Any]:
    """Expediente: documentos/imágenes asociados a un contacto (uploaded_by_contact_id)."""
    from .models import Contact
    from .sessions import sanitize_phone

    p = sanitize_phone(phone)
    with get_session() as s:
        contact = s.scalar(select(Contact).where(Contact.phone == p))
        if contact is None:
            return {"contact_phone": p, "items": []}
        rows = s.scalars(
            select(MediaAsset)
            .where(
                MediaAsset.uploaded_by_contact_id == contact.id,
                MediaAsset.deleted_at.is_(None),
            )
            .order_by(MediaAsset.created_at.desc())
        ).all()
        return {"contact_phone": p, "items": [_to_dict(m) for m in rows]}


def _existing_by_sha(s, sha: str) -> MediaAsset | None:
    return s.scalar(select(MediaAsset).where(MediaAsset.sha256 == sha))


def ingest_from_bytes(
    data: bytes,
    mime_type: str,
    source: str,
    label: str | None = None,
    tags: list[str] | None = None,
    origin_url: str | None = None,
    uploaded_by_contact_id: int | None = None,
    filename_hint: str | None = None,
) -> dict[str, Any]:
    """Persiste un blob a disco + DB. Si sha256 ya existe, devuelve el row existente (dedupe)."""
    _validate_mime(mime_type)
    _validate_size(len(data))
    try:
        src_enum = MediaSource(source)
    except ValueError as e:
        raise MediaError("bad_source", str(e))

    sha = _hash_bytes(data)
    with get_session() as s:
        existing = _existing_by_sha(s, sha)
        if existing is not None:
            # Dedupe: si está soft-deleted, "revive" actualizando deleted_at=null y rewriting el archivo si falta.
            revived = False
            if existing.deleted_at is not None:
                existing.deleted_at = None
                revived = True
            # Re-write archivo si no existe en disco
            sp = Path(existing.storage_path)
            if not sp.exists():
                sp.parent.mkdir(parents=True, exist_ok=True)
                sp.write_bytes(data)
            return {**_to_dict(existing), "dedupe": True, "revived": revived}

        ext = EXT_BY_MIME.get(mime_type, "")
        uid = uuid.uuid4().hex
        storage_dir = _ensure_storage_dir()
        storage_path = storage_dir / f"{uid}{ext}"
        storage_path.write_bytes(data)
        filename = filename_hint or f"{uid}{ext}"

        row = MediaAsset(
            source=src_enum,
            filename=filename,
            mime_type=mime_type,
            size_bytes=len(data),
            sha256=sha,
            storage_path=str(storage_path),
            origin_url=origin_url,
            label=label,
            tags=tags or [],
            uploaded_by_contact_id=uploaded_by_contact_id,
            use_count=0,
        )
        s.add(row)
        s.flush()
        log.info(
            "media ingested id=%s source=%s mime=%s size=%d label=%r",
            row.id, source, mime_type, len(data), label,
        )
        return {**_to_dict(row), "dedupe": False}


def ingest_from_url(
    url: str,
    label: str | None = None,
    tags: list[str] | None = None,
    source: str = "marketing",
    uploaded_by_contact_id: int | None = None,
    enforce_whitelist: bool = True,
) -> dict[str, Any]:
    """Descarga URL whitelisted, valida mime/size, persiste (dedupe por sha256)."""
    if enforce_whitelist and not _is_whitelisted_url(url):
        raise MediaError("not_whitelisted", f"dominio no whitelisted: {url}")
    with httpx.Client(timeout=20.0, follow_redirects=True) as c:
        try:
            r = c.get(url)
            r.raise_for_status()
        except httpx.HTTPError as e:
            raise MediaError("fetch_failed", f"no pude descargar {url}: {e}")
    data = r.content
    # Mime: usar content-type del response, fallback ext de URL
    mime = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIMES:
        # Adivina por extensión URL
        path = urlparse(url).path.lower()
        if path.endswith(".jpg") or path.endswith(".jpeg"):
            mime = "image/jpeg"
        elif path.endswith(".png"):
            mime = "image/png"
        elif path.endswith(".webp"):
            mime = "image/webp"
        elif path.endswith(".pdf"):
            mime = "application/pdf"
    filename_hint = os.path.basename(urlparse(url).path) or None
    return ingest_from_bytes(
        data=data,
        mime_type=mime,
        source=source,
        label=label,
        tags=tags,
        origin_url=url,
        uploaded_by_contact_id=uploaded_by_contact_id,
        filename_hint=filename_hint,
    )


def find_media(query: str, limit: int = 5, source: str | None = None) -> dict[str, Any]:
    """Búsqueda fuzzy por label + tag containment + recency.

    Devuelve {found, count, items}.
    """
    q = (query or "").strip()
    limit = max(1, min(limit, 50))
    with get_session() as s:
        stmt = select(MediaAsset).where(MediaAsset.deleted_at.is_(None))
        if q:
            pattern = f"%{q}%"
            # Tag containment: SQLite/PG ambos soportan castear JSON a texto y buscar.
            # Para JSON arrays, una forma portable es ILIKE sobre el cast a string.
            from sqlalchemy import cast, String as SAString
            stmt = stmt.where(
                or_(
                    MediaAsset.label.ilike(pattern),
                    cast(MediaAsset.tags, SAString).ilike(pattern),
                    MediaAsset.filename.ilike(pattern),
                )
            )
        if source:
            try:
                src_enum = MediaSource(source)
                stmt = stmt.where(MediaAsset.source == src_enum)
            except ValueError:
                pass
        stmt = stmt.order_by(
            MediaAsset.last_used_at.desc().nullslast(),
            MediaAsset.created_at.desc(),
        ).limit(limit)
        rows = list(s.scalars(stmt))
        items = [_to_dict(m) for m in rows]
        return {"found": bool(items), "count": len(items), "items": items}


def get_media(asset_id: int, include_deleted: bool = False) -> dict[str, Any] | None:
    with get_session() as s:
        m = s.get(MediaAsset, asset_id)
        if m is None:
            return None
        if m.deleted_at is not None and not include_deleted:
            return None
        return _to_dict(m)


def get_storage_path(asset_id: int) -> tuple[str, str, str] | None:
    """Devuelve (storage_path, mime_type, filename) o None si no existe / borrado."""
    with get_session() as s:
        m = s.get(MediaAsset, asset_id)
        if m is None or m.deleted_at is not None:
            return None
        return (m.storage_path, m.mime_type, m.filename)


def soft_delete(asset_id: int) -> dict[str, Any]:
    """Borra archivo en disco, marca deleted_at. Mantiene row para FK histórico."""
    with get_session() as s:
        m = s.get(MediaAsset, asset_id)
        if m is None:
            return {"ok": False, "error": "not_found"}
        if m.deleted_at is not None:
            return {"ok": True, "already": True}
        # Borrar archivo en disco (si existe)
        try:
            sp = Path(m.storage_path)
            if sp.exists():
                sp.unlink()
        except Exception:  # noqa: BLE001
            log.exception("no pude borrar archivo %s", m.storage_path)
        m.deleted_at = datetime.now(timezone.utc)
        return {"ok": True, "id": asset_id}


def record_use(asset_id: int) -> None:
    """Incrementa use_count + last_used_at. Tolerante a fallos."""
    try:
        with get_session() as s:
            m = s.get(MediaAsset, asset_id)
            if m is None:
                return
            m.use_count = (m.use_count or 0) + 1
            m.last_used_at = datetime.now(timezone.utc)
    except Exception:  # noqa: BLE001
        log.exception("record_use failed asset_id=%s", asset_id)


# --- Caption ingest parsing -------------------------------------------------

import re as _re

_INGEST_RE = _re.compile(
    r"^\s*(?:guarda|guardar|save)\s+(?:como|as)\s+(?P<label>.+?)\s*$",
    flags=_re.IGNORECASE,
)
_TAG_RE = _re.compile(r"#(\w+)")


def looks_like_ingest_caption(text: str) -> bool:
    if not text:
        return False
    # Quita los tags primero para ver si lo que queda matchea el patrón.
    stripped = _TAG_RE.sub("", text).strip()
    return bool(_INGEST_RE.match(stripped))


def parse_ingest_caption(text: str) -> tuple[str | None, list[str]]:
    """Devuelve (label, tags) o (None, []) si no matchea."""
    if not text:
        return None, []
    tags = _TAG_RE.findall(text)
    stripped = _TAG_RE.sub("", text).strip()
    m = _INGEST_RE.match(stripped)
    if not m:
        return None, tags
    label = m.group("label").strip()
    return label, tags
