"""Iris agéntica — tablas tasks y task_targets + columna wa_jid en contacts.

Revision ID: 0006
Revises: 0005
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # contacts.wa_jid: persiste el JID original (con suffix @s.whatsapp.net o @lid)
    # para que wa-listener pueda enviar a contactos aunque su PHONE_TO_JID in-memory se haya perdido tras restart.
    op.add_column("contacts", sa.Column("wa_jid", sa.String(64), nullable=True))

    # tasks: instrucción dictada por owner (OWNER), puede tener N targets.
    op.create_table(
        "tasks",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("owner_id", sa.BigInteger, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("raw_instruction", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("context", sa.dialects.postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["contacts.id"], name="fk_tasks_owner_id", ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','awaiting_responses','complete','cancelled')",
            name="ck_tasks_status",
        ),
    )
    op.create_index("ix_tasks_owner_id", "tasks", ["owner_id"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    # task_targets: un row por destinatario de la task.
    op.create_table(
        "task_targets",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("task_id", sa.BigInteger, nullable=False),
        sa.Column("contact_id", sa.BigInteger, nullable=False),
        sa.Column("message_sent", sa.Text, nullable=True),
        sa.Column("message_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("thread_id", sa.BigInteger, nullable=True),
        sa.Column("response", sa.Text, nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_classification", sa.Text, nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], name="fk_task_targets_task_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], name="fk_task_targets_contact_id", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], name="fk_task_targets_thread_id", ondelete="SET NULL"),
        sa.CheckConstraint(
            "status IN ('pending','sent','responded','failed','cancelled')",
            name="ck_task_targets_status",
        ),
        sa.CheckConstraint(
            "response_classification IS NULL OR response_classification IN ('accepted','declined','maybe','clarify','other','no_response')",
            name="ck_task_targets_response_class",
        ),
    )
    op.create_index("ix_task_targets_task_id", "task_targets", ["task_id"])
    op.create_index("ix_task_targets_contact_status", "task_targets", ["contact_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_task_targets_contact_status", table_name="task_targets")
    op.drop_index("ix_task_targets_task_id", table_name="task_targets")
    op.drop_table("task_targets")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_index("ix_tasks_owner_id", table_name="tasks")
    op.drop_table("tasks")
    op.drop_column("contacts", "wa_jid")
