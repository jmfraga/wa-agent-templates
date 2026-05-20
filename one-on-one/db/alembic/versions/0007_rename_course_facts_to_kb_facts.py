"""Rename kb_facts → kb_facts (generalización para multi-vertical).

Iris originalmente almacenaba 'kb_facts' porque arrancó para ExampleCorp/cursos.
Para reusar el patrón en otros negocios (legal, salud, consultoría, ventas)
el término correcto es Knowledge Base. Renombramos:

- Table: kb_facts → kb_facts
- Column: kb_slug → kb_slug
- Indexes correspondientes

Schema 100% compatible — solo cambia naming. Datos quedan idénticos.

Revision ID: 0007
Revises: 0006
"""
from __future__ import annotations

from alembic import op


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER INDEX IF EXISTS ix_course_facts_course_slug RENAME TO ix_kb_facts_kb_slug")
    op.execute("ALTER TABLE kb_facts RENAME COLUMN kb_slug TO kb_slug")
    op.execute("ALTER TABLE kb_facts RENAME CONSTRAINT ck_course_facts_source TO ck_kb_facts_source")
    op.execute("ALTER TABLE kb_facts RENAME CONSTRAINT uq_course_facts_slug_key TO uq_kb_facts_slug_key")
    op.execute("ALTER TABLE kb_facts RENAME CONSTRAINT course_facts_pkey TO kb_facts_pkey")
    op.execute("ALTER TABLE kb_facts RENAME TO kb_facts")


def downgrade() -> None:
    op.execute("ALTER TABLE kb_facts RENAME TO kb_facts")
    op.execute("ALTER TABLE kb_facts RENAME COLUMN kb_slug TO kb_slug")
    op.execute("ALTER TABLE kb_facts RENAME CONSTRAINT ck_kb_facts_source TO ck_course_facts_source")
    op.execute("ALTER TABLE kb_facts RENAME CONSTRAINT uq_kb_facts_slug_key TO uq_course_facts_slug_key")
    op.execute("ALTER TABLE kb_facts RENAME CONSTRAINT kb_facts_pkey TO course_facts_pkey")
    op.execute("ALTER INDEX IF EXISTS ix_kb_facts_kb_slug RENAME TO ix_course_facts_course_slug")
