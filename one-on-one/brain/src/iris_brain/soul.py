"""Carga SOUL.md como bloque system con cache_control ephemeral."""
from __future__ import annotations

import time
from pathlib import Path

from .config import settings

_cache: dict = {"text": None, "loaded_at": 0.0, "mtime": 0.0}
_RELOAD_INTERVAL_S = 60.0


def _read_soul() -> str:
    p: Path = settings.IRIS_BRAIN_SOUL_PATH
    now = time.time()
    if _cache["text"] is not None and now - _cache["loaded_at"] < _RELOAD_INTERVAL_S:
        return _cache["text"]
    try:
        st = p.stat()
        if _cache["text"] is None or st.st_mtime > _cache["mtime"]:
            _cache["text"] = p.read_text(encoding="utf-8")
            _cache["mtime"] = st.st_mtime
        _cache["loaded_at"] = now
    except OSError:
        if _cache["text"] is None:
            _cache["text"] = (
                "Eres Iris, asistente del owner. "
                "SOUL.md no se pudo cargar; usa tono cálido y escala todo a OWNER."
            )
    return _cache["text"]


def load_soul() -> dict:
    """Devuelve el bloque system con cache_control ephemeral."""
    return {"type": "text", "text": _read_soul(), "cache_control": {"type": "ephemeral"}}


def soul_text() -> str:
    return _read_soul()


def force_reload() -> str:
    _cache["loaded_at"] = 0.0
    return _read_soul()


def reload() -> str:
    """Alias usado por el admin. Fuerza recarga desde disco."""
    _cache["text"] = None
    _cache["mtime"] = 0.0
    _cache["loaded_at"] = 0.0
    return _read_soul()


_MAX_BACKUPS = 10


def save(text: str, updated_by: str | None = None) -> dict:
    """Escribe SOUL.md atomicamente con backup. Mantiene últimos N backups.

    Returns: {path, size_bytes, backup_path}
    """
    import os
    import time
    from datetime import datetime, timezone

    p: Path = settings.IRIS_BRAIN_SOUL_PATH
    p = p if p.is_absolute() else Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    if p.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = p.with_suffix(p.suffix + f".bak.{ts}")
        backup_path.write_bytes(p.read_bytes())

    # Atomic write: tmp + rename.
    tmp = p.with_suffix(p.suffix + f".tmp.{int(time.time()*1000)}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)

    # Limpiar backups viejos.
    try:
        backups = sorted(
            p.parent.glob(p.name + ".bak.*"),
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        for old in backups[_MAX_BACKUPS:]:
            try:
                old.unlink()
            except OSError:
                pass
    except OSError:
        pass

    reload()
    return {
        "path": str(p),
        "size_bytes": p.stat().st_size,
        "backup_path": str(backup_path) if backup_path else None,
    }


def soul_info() -> dict:
    """Metadata para /admin/soul GET."""
    p: Path = settings.IRIS_BRAIN_SOUL_PATH
    p = p if p.is_absolute() else Path.cwd() / p
    try:
        st = p.stat()
        return {
            "text": _read_soul(),
            "path": str(p),
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
        }
    except OSError:
        return {
            "text": _read_soul(),
            "path": str(p),
            "size_bytes": 0,
            "mtime": 0.0,
        }
