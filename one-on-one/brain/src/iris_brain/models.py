"""ORM models para Iris v2. 6 tablas + enums."""
from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _enum_col(enum_cls, name, default=None):
    """SQLAlchemy Enum que serializa con .value (no .name) y usa CHECK constraint en DB."""
    kwargs = dict(
        name=name,
        values_callable=lambda obj: [m.value for m in obj],
        native_enum=False,  # CHECK constraint, no native PG ENUM type
        length=64,
    )
    if default is not None:
        return mapped_column(Enum(enum_cls, **kwargs), default=default)
    return mapped_column(Enum(enum_cls, **kwargs))


class ContactKind(str, enum.Enum):
    owner = "owner"
    paciente = "paciente"
    prospecto_curso = "prospecto_curso"
    asesoria = "asesoria"
    colega = "colega"
    amigo = "amigo"
    familia = "familia"
    otro = "otro"


class MessageDirection(str, enum.Enum):
    in_ = "in"
    out = "out"


class TicketStatus(str, enum.Enum):
    open = "open"
    awaiting_jmf = "awaiting_owner"
    awaiting_patient = "awaiting_patient"
    closed = "closed"


class ThreadStatus(str, enum.Enum):
    open = "open"
    closed = "closed"


class KbFactSource(str, enum.Enum):
    owner = "owner"
    landing = "landing"


class MediaSource(str, enum.Enum):
    marketing = "marketing"
    ui_upload = "ui_upload"
    telegram = "telegram"
    whatsapp = "whatsapp"


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str | None] = mapped_column(String(255))
    kind: Mapped[ContactKind] = _enum_col(ContactKind, "contact_kind", default=ContactKind.otro)
    notes: Mapped[str | None] = mapped_column(Text)
    wa_jid: Mapped[str | None] = mapped_column(String(64))  # JID original WA (con suffix @lid o @s.whatsapp.net)
    first_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    threads: Mapped[list["Thread"]] = relationship(back_populates="contact")


class Thread(Base):
    __tablename__ = "threads"
    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32), default="whatsapp")
    status: Mapped[ThreadStatus] = _enum_col(ThreadStatus, "thread_status", default=ThreadStatus.open)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    contact: Mapped["Contact"] = relationship(back_populates="threads")
    messages: Mapped[list["Message"]] = relationship(back_populates="thread")
    tickets: Mapped[list["Ticket"]] = relationship(back_populates="thread")


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id"), index=True)
    direction: Mapped[MessageDirection] = _enum_col(MessageDirection, "message_direction")
    body: Mapped[str] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(String(1024))
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    model_used: Mapped[str | None] = mapped_column(String(64))
    tokens_input: Mapped[int | None] = mapped_column(Integer)
    tokens_output: Mapped[int | None] = mapped_column(Integer)
    # Phase 1c — media outbound (imagen híbrida en tasks agénticas)
    media_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    media_caption: Mapped[str | None] = mapped_column(Text)

    thread: Mapped["Thread"] = relationship(back_populates="messages")


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("threads.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))  # libre: 'agenda', 'precio', 'urgencia', etc.
    summary: Mapped[str] = mapped_column(Text)
    draft_for_owner: Mapped[str | None] = mapped_column(Text)
    status: Mapped[TicketStatus] = _enum_col(TicketStatus, "ticket_status", default=TicketStatus.open)
    owner_response: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    thread: Mapped["Thread"] = relationship(back_populates="tickets")


class KbFact(Base):
    __tablename__ = "kb_facts"
    id: Mapped[int] = mapped_column(primary_key=True)
    kb_slug: Mapped[str] = mapped_column(String(64), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[KbFactSource] = _enum_col(KbFactSource, "kb_fact_source", default=KbFactSource.owner)
    ttl_days: Mapped[int | None] = mapped_column(Integer, server_default="90")  # días, None = sin expiración
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class RuntimeConfig(Base):
    """Overrides en caliente de settings (admin UI). key/value plano.

    Si una key existe acá, sobreescribe el valor de env vars.
    """

    __tablename__ = "runtime_config"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    updated_by: Mapped[str | None] = mapped_column(String(128))


class IntentLog(Base):
    __tablename__ = "intents_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int] = mapped_column(ForeignKey("messages.id"), index=True)
    intent: Mapped[str] = mapped_column(String(64))
    confidence: Mapped[float] = mapped_column()
    model_used: Mapped[str] = mapped_column(String(64))
    reasoning: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Task(Base):
    """Tarea agéntica dictada por owner (owner). Puede tener N targets."""
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64))  # invitar|coordinar_cita|enviar_info|otro
    summary: Mapped[str] = mapped_column(Text)
    raw_instruction: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MediaAsset(Base):
    """Asset multimedia (imagen / pdf) reutilizable en tasks agénticas.

    3 fuentes: marketing (URL whitelisted), ui_upload, telegram (owner), whatsapp (owner).
    Dedupe por sha256. Soft-delete vía deleted_at.
    """
    __tablename__ = "media_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[MediaSource] = _enum_col(MediaSource, "media_source")
    filename: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(Text, unique=True)
    storage_path: Mapped[str] = mapped_column(Text)
    origin_url: Mapped[str | None] = mapped_column(Text)
    label: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSON)
    uploaded_by_contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    use_count: Mapped[int] = mapped_column(Integer, default=0)


class TaskTarget(Base):
    """Un row por destinatario de una task."""
    __tablename__ = "task_targets"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    message_sent: Mapped[str | None] = mapped_column(Text)
    message_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    thread_id: Mapped[int | None] = mapped_column(ForeignKey("threads.id"))
    response: Mapped[str | None] = mapped_column(Text)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response_classification: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
