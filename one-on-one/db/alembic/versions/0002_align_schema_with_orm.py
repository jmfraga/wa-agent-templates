"""Align tickets and kb_facts schema with ORM.

Sprint 2 reveló divergencias entre la migración inicial 0001 (escrita por agente C)
y el ORM en iris_brain.models (escrito por agente B):

- tickets: ORM tiene draft_for_jmf, migración no. Lo agregamos.
- kb_facts: ORM ahora tiene ttl_days (no ttl). Ya estaba bien en migración.
- threads: el CHECK ck_threads_status se ajusta a 'open|closed' (no active|closed).
  Ya se ajustó vía ALTER manual; aquí lo dejamos versionado para futuras DBs limpias.

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tickets.draft_for_jmf
    op.add_column(
        "tickets",
        sa.Column("draft_for_jmf", sa.Text(), nullable=True),
    )

    # Realinear CHECK de threads.status: 'open|closed' (no 'active|closed')
    op.drop_constraint("ck_threads_status", "threads", type_="check")
    op.create_check_constraint(
        "ck_threads_status",
        "threads",
        "status IN ('open','closed')",
    )


def downgrade() -> None:
    op.drop_column("tickets", "draft_for_jmf")
    op.drop_constraint("ck_threads_status", "threads", type_="check")
    op.create_check_constraint(
        "ck_threads_status",
        "threads",
        "status IN ('active','closed')",
    )
