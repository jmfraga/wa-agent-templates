"""Iris media outbound (Phase 1c) — tabla media_assets + columnas en messages.

Storage físico en /var/lib/iris/media/{uuid}.{ext}; metadata acá.
Source enum: 'marketing' (URL whitelisted) | 'ui_upload' | 'telegram' | 'whatsapp'.
Dedupe por sha256. Soft-delete vía deleted_at.

Revision ID: 0008
Revises: 0007
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_assets",
        sa.Column("id", sa.BigInteger, sa.Identity(always=True), primary_key=True),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("mime_type", sa.Text, nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.Text, nullable=False, unique=True),
        sa.Column("storage_path", sa.Text, nullable=False),
        sa.Column("origin_url", sa.Text, nullable=True),
        sa.Column("label", sa.Text, nullable=True),
        sa.Column("tags", sa.JSON, nullable=True),
        sa.Column("uploaded_by_contact_id", sa.BigInteger, nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("use_count", sa.Integer, nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(
            ["uploaded_by_contact_id"], ["contacts.id"],
            name="fk_media_assets_uploaded_by", ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "source IN ('marketing','ui_upload','telegram','whatsapp')",
            name="ck_media_assets_source",
        ),
        sa.CheckConstraint(
            "mime_type IN ('image/jpeg','image/png','image/webp','application/pdf')",
            name="ck_media_assets_mime",
        ),
    )
    op.create_index("ix_media_assets_label", "media_assets", ["label"])
    op.create_index("ix_media_assets_created", "media_assets", ["created_at"])
    op.create_index("ix_media_assets_source", "media_assets", ["source"])

    op.add_column(
        "messages",
        sa.Column("media_asset_id", sa.BigInteger, nullable=True),
    )
    op.add_column("messages", sa.Column("media_caption", sa.Text, nullable=True))
    op.create_foreign_key(
        "fk_messages_media_asset_id",
        "messages",
        "media_assets",
        ["media_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_messages_media_asset_id", "messages", type_="foreignkey")
    op.drop_column("messages", "media_caption")
    op.drop_column("messages", "media_asset_id")
    op.drop_index("ix_media_assets_source", table_name="media_assets")
    op.drop_index("ix_media_assets_created", table_name="media_assets")
    op.drop_index("ix_media_assets_label", table_name="media_assets")
    op.drop_table("media_assets")
