"""Add tasks.archived_at (archivado manual o automático del Kanban)

Revision ID: 016
Revises: 015
Create Date: 2026-09-07
"""
import sqlalchemy as sa
from alembic import op

revision = "016"
down_revision = "015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tasks",
        sa.Column("archived_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tasks", "archived_at")
