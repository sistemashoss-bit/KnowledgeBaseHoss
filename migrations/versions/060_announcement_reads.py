"""Comunicados: confirmación de visto. documents.requires_ack marca los que
piden "Enterado"; announcement_reads guarda quién lo abrió y quién confirmó.

Revision ID: 060
Revises: 059
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "060"
down_revision = "059"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("requires_ack", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "announcement_reads",
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_announcement_reads_user_id", "announcement_reads", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_announcement_reads_user_id", table_name="announcement_reads")
    op.drop_table("announcement_reads")
    op.drop_column("documents", "requires_ack")
