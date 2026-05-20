"""Audit log writes. Persistencia best-effort: si falla, no debe romper el chat."""
import json
import logging
from typing import Any, Optional

from .db import get_session
from .models import AuditLog

_log = logging.getLogger(__name__)


def log(
    kind: str,
    *,
    group_jid: Optional[str] = None,
    payload: Optional[dict[str, Any]] = None,
) -> None:
    try:
        with get_session() as s:
            s.add(
                AuditLog(
                    kind=kind,
                    group_jid=group_jid,
                    payload=json.dumps(payload or {}, ensure_ascii=False, default=str),
                )
            )
            s.commit()
    except Exception:  # noqa: BLE001
        _log.exception("audit log write failed (kind=%s)", kind)
