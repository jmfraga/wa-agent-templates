"""Iris v2 — audit log de ingest desde URL.

Revision ID: 0011
Revises: 0010

Crea `kb_ingest_log` para registrar cada llamada a /admin/kb-facts/ingest-url:
url, slug resuelto, dry_run, facts_count, tokens y costo estimado, error si
hubo, timestamp. Permite auditoría + UI /admin/kb-ingest-log.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kb_ingest_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("slug", sa.String(64), nullable=True),
        sa.Column(
            "dry_run", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("facts_count", sa.Integer(), nullable=True),
        sa.Column("tokens_input", sa.Integer(), nullable=True),
        sa.Column("tokens_output", sa.Integer(), nullable=True),
        sa.Column("cost_usd_estimate", sa.Numeric(10, 6), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("triggered_by", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_kb_ingest_log_created_at",
        "kb_ingest_log",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_kb_ingest_log_created_at", table_name="kb_ingest_log")
    op.drop_table("kb_ingest_log")
