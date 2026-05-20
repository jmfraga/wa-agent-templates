"""Expand ContactKind: agrega 'colega' (salud/educación), 'amigo', 'familia'.

Revision ID: 0004
Revises: 0003
"""
from __future__ import annotations

from alembic import op


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_contacts_kind", "contacts", type_="check")
    op.create_check_constraint(
        "ck_contacts_kind",
        "contacts",
        "kind IN ('paciente','prospecto_curso','asesoria','colega','amigo','familia','otro')",
    )


def downgrade() -> None:
    op.execute("UPDATE contacts SET kind='otro' WHERE kind IN ('colega','amigo','familia')")
    op.drop_constraint("ck_contacts_kind", "contacts", type_="check")
    op.create_check_constraint(
        "ck_contacts_kind",
        "contacts",
        "kind IN ('paciente','prospecto_curso','asesoria','otro')",
    )
