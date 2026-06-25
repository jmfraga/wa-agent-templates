import logging
from typing import Iterable

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .config import settings
from .models import Base

_log = logging.getLogger(__name__)

connect_args = {"check_same_thread": False} if settings.db_url.startswith("sqlite") else {}
engine = create_engine(settings.db_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


# Columnas añadidas post-init que necesitan ALTER en DBs existentes.
# (Mantén esta lista corta; cuando migremos a Postgres real, pasamos a Alembic.)
_LIGHTWEIGHT_MIGRATIONS: list[tuple[str, str, str]] = [
    # (tabla, columna, DDL)
    ("groups", "last_proactive_at", "ALTER TABLE groups ADD COLUMN last_proactive_at DATETIME"),
    # Candado de autorización. DEFAULT 1 → los grupos existentes quedan autorizados y
    # marcados como ya notificados (nunca DM por ellos). Los grupos nuevos los crea el
    # ORM con is_authorized=False / owner_notified=False (ver models.py + chat.py).
    ("groups", "is_authorized", "ALTER TABLE groups ADD COLUMN is_authorized BOOLEAN NOT NULL DEFAULT 1"),
    ("groups", "owner_notified", "ALTER TABLE groups ADD COLUMN owner_notified BOOLEAN NOT NULL DEFAULT 1"),
]


def _apply_lightweight_migrations() -> None:
    insp = inspect(engine)
    if not insp.has_table("groups"):
        return  # create_all aún no se ha llamado
    for table, column, ddl in _LIGHTWEIGHT_MIGRATIONS:
        if not insp.has_table(table):
            continue
        cols = {c["name"] for c in insp.get_columns(table)}
        if column in cols:
            continue
        try:
            with engine.begin() as conn:
                conn.execute(text(ddl))
            _log.info("applied lightweight migration: %s", ddl)
        except Exception:  # noqa: BLE001
            _log.exception("lightweight migration failed: %s", ddl)


def create_all() -> None:
    Base.metadata.create_all(engine)
    _apply_lightweight_migrations()


def get_session() -> Session:
    return SessionLocal()
