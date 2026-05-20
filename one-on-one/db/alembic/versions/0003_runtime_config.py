"""runtime_config table for admin overrides

Sprint 3: capa admin para configurar Iris en caliente sin editar .env ni
reiniciar uvicorn. Tabla key/value plana, fallback al env.

Revision ID: 0003
Revises: 0002
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runtime_config",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("runtime_config")
