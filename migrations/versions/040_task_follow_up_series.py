"""Add task_follow_ups.series_id (seguimientos repetidos)

Revision ID: 040
Revises: 039
Create Date: 2026-10-24
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "040"
down_revision = "039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("task_follow_ups", sa.Column("series_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_task_follow_ups_series_id", "task_follow_ups", ["series_id"])


def downgrade() -> None:
    op.drop_index("ix_task_follow_ups_series_id", table_name="task_follow_ups")
    op.drop_column("task_follow_ups", "series_id")
