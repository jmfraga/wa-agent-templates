"""Agrega kind='owner' para Dr. Owner (creador y dueño).

Revision ID: 0005
Revises: 0004
"""
from __future__ import annotations

from alembic import op


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_contacts_kind", "contacts", type_="check")
    op.create_check_constraint(
        "ck_contacts_kind",
        "contacts",
        "kind IN ('owner','paciente','prospecto_curso','asesoria','colega','amigo','familia','otro')",
    )


def downgrade() -> None:
    op.execute("UPDATE contacts SET kind='familia' WHERE kind='owner'")
    op.drop_constraint("ck_contacts_kind", "contacts", type_="check")
    op.create_check_constraint(
        "ck_contacts_kind",
        "contacts",
        "kind IN ('paciente','prospecto_curso','asesoria','colega','amigo','familia','otro')",
    )
