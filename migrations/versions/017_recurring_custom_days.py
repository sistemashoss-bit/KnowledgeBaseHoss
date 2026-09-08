"""Add recurring_tasks.days_of_week (frecuencia "días específicos")

Revision ID: 017
Revises: 016
Create Date: 2026-09-08
"""
import sqlalchemy as sa
from alembic import op

revision = "017"
down_revision = "016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recurring_tasks",
        sa.Column("days_of_week", sa.String(20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("recurring_tasks", "days_of_week")
