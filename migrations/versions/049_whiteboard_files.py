"""Imágenes de las pizarras en su bucket (fuera del documento Yjs)

Revision ID: 049
Revises: 048
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "049"
down_revision = "048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whiteboard_files",
        sa.Column("board_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("whiteboards.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("file_id", sa.String(100), primary_key=True),
        sa.Column("storage_key", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("whiteboard_files")
