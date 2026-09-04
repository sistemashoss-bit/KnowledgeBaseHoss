"""Add task_comments.edited flag (marca "editado" en comentarios)

Revision ID: 015
Revises: 014
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = "015"
down_revision = "014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "task_comments",
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("task_comments", "edited")
