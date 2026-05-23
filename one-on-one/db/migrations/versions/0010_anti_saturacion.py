"""Iris v2 — anti-saturación + silenciamiento por contacto.

Revision ID: 0010
Revises: 0009

Añade a `contacts`:
- silent_mode (bool, default false) — si true, Iris reporta entrantes al owner
  pero no contesta; tampoco se acepta outbound.
- paused_until (timestamptz) — Iris no envía a este contacto hasta esa fecha.
- last_outbound_at (timestamptz) — última vez que Iris mandó algo.
- outbound_count_24h (int, default 0) — contador rolling 24h.
- outbound_count_reset_at (timestamptz) — momento desde el que cuenta el rolling.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contacts",
        sa.Column("silent_mode", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "contacts",
        sa.Column("paused_until", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "contacts",
        sa.Column(
            "outbound_count_24h",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.add_column(
        "contacts",
        sa.Column("outbound_count_reset_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("contacts", "outbound_count_reset_at")
    op.drop_column("contacts", "outbound_count_24h")
    op.drop_column("contacts", "last_outbound_at")
    op.drop_column("contacts", "paused_until")
    op.drop_column("contacts", "silent_mode")
