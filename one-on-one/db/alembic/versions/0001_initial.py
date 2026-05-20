"""initial schema: contacts, threads, messages, tickets, kb_facts, intents_log

Revision ID: 0001
Revises:
Create Date: 2026-05-13

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # contacts
    op.create_table(
        "contacts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("phone", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "first_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_seen",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("phone", name="uq_contacts_phone"),
        sa.CheckConstraint(
            "kind IN ('paciente','prospecto_curso','asesoria','otro')",
            name="ck_contacts_kind",
        ),
    )
    op.create_index("ix_contacts_phone", "contacts", ["phone"])
    op.create_index("ix_contacts_kind", "contacts", ["kind"])

    # threads
    op.create_table(
        "threads",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("contact_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "channel",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'whatsapp'"),
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "opened_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["contact_id"],
            ["contacts.id"],
            name="fk_threads_contact_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('active','closed')",
            name="ck_threads_status",
        ),
    )
    op.create_index("ix_threads_contact_id", "threads", ["contact_id"])
    op.create_index("ix_threads_status", "threads", ["status"])

    # messages
    op.create_table(
        "messages",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("media_url", sa.Text(), nullable=True),
        sa.Column(
            "ts",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("model_used", sa.Text(), nullable=True),
        sa.Column("tokens_input", sa.Integer(), nullable=True),
        sa.Column("tokens_output", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["threads.id"],
            name="fk_messages_thread_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "direction IN ('in','out')",
            name="ck_messages_direction",
        ),
    )
    op.create_index("ix_messages_thread_id", "messages", ["thread_id"])
    op.create_index("ix_messages_ts", "messages", ["ts"])

    # tickets
    op.create_table(
        "tickets",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("jmf_response", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["thread_id"],
            ["threads.id"],
            name="fk_tickets_thread_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "status IN ('open','awaiting_jmf','awaiting_patient','closed')",
            name="ck_tickets_status",
        ),
    )
    op.create_index("ix_tickets_status", "tickets", ["status"])
    op.create_index("ix_tickets_thread_id", "tickets", ["thread_id"])

    # kb_facts
    op.create_table(
        "kb_facts",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("kb_slug", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("ttl_days", sa.Integer(), nullable=True, server_default=sa.text("90")),
        sa.Column(
            "version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "kb_slug", "key", name="uq_course_facts_slug_key"
        ),
        sa.CheckConstraint(
            "source IN ('jmf','landing','manual')",
            name="ck_course_facts_source",
        ),
    )
    op.create_index("ix_course_facts_course_slug", "kb_facts", ["kb_slug"])

    # intents_log
    op.create_table(
        "intents_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("confidence", sa.REAL(), nullable=True),
        sa.Column("model_used", sa.Text(), nullable=True),
        sa.Column("reasoning", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["message_id"],
            ["messages.id"],
            name="fk_intents_log_message_id",
            ondelete="CASCADE",
        ),
    )
    op.create_index("ix_intents_log_intent", "intents_log", ["intent"])
    op.create_index("ix_intents_log_message_id", "intents_log", ["message_id"])


def downgrade() -> None:
    op.drop_index("ix_intents_log_message_id", table_name="intents_log")
    op.drop_index("ix_intents_log_intent", table_name="intents_log")
    op.drop_table("intents_log")

    op.drop_index("ix_course_facts_course_slug", table_name="kb_facts")
    op.drop_table("kb_facts")

    op.drop_index("ix_tickets_thread_id", table_name="tickets")
    op.drop_index("ix_tickets_status", table_name="tickets")
    op.drop_table("tickets")

    op.drop_index("ix_messages_ts", table_name="messages")
    op.drop_index("ix_messages_thread_id", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_threads_status", table_name="threads")
    op.drop_index("ix_threads_contact_id", table_name="threads")
    op.drop_table("threads")

    op.drop_index("ix_contacts_kind", table_name="contacts")
    op.drop_index("ix_contacts_phone", table_name="contacts")
    op.drop_table("contacts")
