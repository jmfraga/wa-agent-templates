"""Iris Phase 1c+ — tasks.scheduled_at para programación diferida.

Revision ID: 0009
Revises: 0008
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Index parcial: solo pending + scheduled (worker query muy frecuente).
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_tasks_scheduled "
        "ON tasks(scheduled_at) "
        "WHERE status='pending' AND scheduled_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_tasks_scheduled")
    op.drop_column("tasks", "scheduled_at")
