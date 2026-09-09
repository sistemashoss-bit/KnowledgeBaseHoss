"""Add document_allowed_users (visibilidad 'custom': personas específicas del depto)

Revision ID: 019
Revises: 018
Create Date: 2026-09-09
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "019"
down_revision = "018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_allowed_users",
        sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("added_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_document_allowed_users_user_id", "document_allowed_users", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_document_allowed_users_user_id", table_name="document_allowed_users")
    op.drop_table("document_allowed_users")
