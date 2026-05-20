"""SQLAlchemy ORM. Agnóstico SQLite/Postgres."""
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Enums como strings simples — más portable que ENUM nativo de Postgres en SQLite.
GROUP_MODES = ("lurker", "proactive", "on_command_only")
MSG_DIRECTIONS = ("in", "out")
FACT_SOURCES = ("jmf", "auto", "grupo")
DISCUSSION_STATUS = ("draft", "scheduled", "posted", "cancelled")


def _now() -> datetime:
    return datetime.utcnow()


class Group(Base):
    __tablename__ = "groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_jid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32), default="lurker")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_proactive_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    souls: Mapped[list["GroupSoul"]] = relationship(back_populates="group", cascade="all, delete-orphan")
    messages: Mapped[list["Message"]] = relationship(back_populates="group")
    kbs: Mapped[list["GroupKb"]] = relationship(back_populates="group", cascade="all, delete-orphan")


class GroupSoul(Base):
    __tablename__ = "group_souls"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"), index=True)
    soul_md: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    group: Mapped[Group] = relationship(back_populates="souls")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    facts: Mapped[list["KbFact"]] = relationship(back_populates="kb", cascade="all, delete-orphan")
    groups: Mapped[list["GroupKb"]] = relationship(back_populates="kb")


class KbFact(Base):
    __tablename__ = "kb_facts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32), default="jmf")
    version: Mapped[int] = mapped_column(Integer, default=1)
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=_now)
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")  # active|pending_review|expired

    kb: Mapped[KnowledgeBase] = relationship(back_populates="facts")


class GroupKb(Base):
    __tablename__ = "group_kbs"
    __table_args__ = (UniqueConstraint("group_id", "kb_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id", ondelete="CASCADE"))
    priority: Mapped[int] = mapped_column(Integer, default=0)

    group: Mapped[Group] = relationship(back_populates="kbs")
    kb: Mapped[KnowledgeBase] = relationship(back_populates="groups")


class Contact(Base):
    __tablename__ = "contacts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    wa_jid: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[Optional[int]] = mapped_column(ForeignKey("groups.id"), nullable=True, index=True)
    contact_jid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True, index=True)
    contact_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    direction: Mapped[str] = mapped_column(String(8))  # in|out
    body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    media_hint: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    quoted_msg_id: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    mentions_phoenix: Mapped[bool] = mapped_column(Boolean, default=False)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    model_used: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tokens_in: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    tokens_out: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    group: Mapped[Optional[Group]] = relationship(back_populates="messages")


class DiscussionStarter(Base):
    __tablename__ = "discussion_starters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("groups.id", ondelete="CASCADE"))
    topic: Mapped[str] = mapped_column(Text)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    posted_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    triggered_by: Mapped[str] = mapped_column(String(128), default="jmf")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class AppConfig(Base):
    """Key/value config persistente, sobrescribe .env en runtime."""
    __tablename__ = "app_config"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=_now, index=True)
    kind: Mapped[str] = mapped_column(String(64), index=True)  # proactive_trigger | fact_recall | escalation | gating_decision | error
    group_jid: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    payload: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # JSON serialized
