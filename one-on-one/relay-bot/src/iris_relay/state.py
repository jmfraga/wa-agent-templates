"""SQLite-backed mapping of ticket_id ↔ Telegram message coordinates."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    String,
    Text,
    create_engine,
    select,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class TicketTelegramMap(Base):
    __tablename__ = "ticket_telegram_map"

    ticket_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    thread_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    kind: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="awaiting_jmf")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )


class PendingAction(Base):
    """Estado libre por chat para callbacks que esperan input siguiente.

    Tipos:
    - usr_reply       payload={"contact_phone": "..."}
    - plan_edit       payload={"task_id": N}
    - plan_sched_custom payload={"task_id": N}
    - iris_silence    payload={}
    """

    __tablename__ = "pending_action"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class StateStore:
    """Thin wrapper around the SQLAlchemy session for the relay's local state."""

    def __init__(self, db_url: str):
        self.engine = create_engine(db_url, future=True, connect_args={"check_same_thread": False})
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        Base.metadata.create_all(self.engine)

    def session(self) -> Session:
        return self.SessionLocal()

    # --- Operations -----------------------------------------------------

    def upsert_ticket(
        self,
        ticket_id: int,
        chat_id: int,
        message_id: int,
        thread_id: Optional[int] = None,
        kind: Optional[str] = None,
        status: str = "awaiting_jmf",
    ) -> TicketTelegramMap:
        with self.session() as s:
            row = s.get(TicketTelegramMap, ticket_id)
            if row is None:
                row = TicketTelegramMap(
                    ticket_id=ticket_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    thread_id=thread_id,
                    kind=kind,
                    status=status,
                )
                s.add(row)
            else:
                row.chat_id = chat_id
                row.message_id = message_id
                row.thread_id = thread_id
                row.kind = kind
                row.status = status
            s.commit()
            s.refresh(row)
            return row

    def get_ticket(self, ticket_id: int) -> Optional[TicketTelegramMap]:
        with self.session() as s:
            return s.get(TicketTelegramMap, ticket_id)

    def find_by_message(self, chat_id: int, message_id: int) -> Optional[TicketTelegramMap]:
        with self.session() as s:
            stmt = select(TicketTelegramMap).where(
                TicketTelegramMap.chat_id == chat_id,
                TicketTelegramMap.message_id == message_id,
            )
            return s.execute(stmt).scalar_one_or_none()

    def set_status(self, ticket_id: int, status: str) -> None:
        with self.session() as s:
            row = s.get(TicketTelegramMap, ticket_id)
            if row is not None:
                row.status = status
                s.commit()

    def find_awaiting_reply(self, chat_id: int) -> Optional[TicketTelegramMap]:
        """Devuelve el ticket en estado awaiting_reply más reciente para ese chat.

        Permite que Owner presione ✍️ Responder y luego mande un texto plano
        (sin usar la función Reply nativa de Telegram).
        """
        with self.session() as s:
            stmt = (
                select(TicketTelegramMap)
                .where(
                    TicketTelegramMap.chat_id == chat_id,
                    TicketTelegramMap.status == "awaiting_reply",
                )
                .order_by(TicketTelegramMap.updated_at.desc())
                .limit(1)
            )
            return s.execute(stmt).scalar_one_or_none()

    # --- Pending actions (callbacks que esperan input) -----------------

    def set_pending_action(self, chat_id: int, action: str, payload: dict | None = None) -> None:
        import json as _json
        with self.session() as s:
            row = s.get(PendingAction, chat_id)
            if row is None:
                row = PendingAction(chat_id=chat_id, action=action, payload=_json.dumps(payload or {}))
                s.add(row)
            else:
                row.action = action
                row.payload = _json.dumps(payload or {})
                row.created_at = datetime.utcnow()
            s.commit()

    def pop_pending_action(self, chat_id: int) -> tuple[str, dict] | None:
        import json as _json
        with self.session() as s:
            row = s.get(PendingAction, chat_id)
            if row is None:
                return None
            action = row.action
            try:
                payload = _json.loads(row.payload or "{}")
            except Exception:  # noqa: BLE001
                payload = {}
            s.delete(row)
            s.commit()
            return action, payload

    def get_pending_action(self, chat_id: int) -> tuple[str, dict] | None:
        import json as _json
        with self.session() as s:
            row = s.get(PendingAction, chat_id)
            if row is None:
                return None
            try:
                payload = _json.loads(row.payload or "{}")
            except Exception:  # noqa: BLE001
                payload = {}
            return row.action, payload

    def clear_pending_action(self, chat_id: int) -> None:
        with self.session() as s:
            row = s.get(PendingAction, chat_id)
            if row is not None:
                s.delete(row)
                s.commit()

    def count_pending(self) -> int:
        with self.session() as s:
            stmt = select(TicketTelegramMap).where(
                TicketTelegramMap.status.in_(("awaiting_jmf", "awaiting_reply"))
            )
            return len(s.execute(stmt).scalars().all())
