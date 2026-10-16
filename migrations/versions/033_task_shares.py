"""Add task_shares (compartir una tarea para seguimiento y comentarios)

Revision ID: 033
Revises: 032
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "033"
down_revision = "032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_shares",
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("shared_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_task_shares_user_id", "task_shares", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_task_shares_user_id", table_name="task_shares")
    op.drop_table("task_shares")
