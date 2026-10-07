"""Widget de reportes: tasks.source_app, tasks.context y departments.triage_description

Revision ID: 029
Revises: 028
Create Date: 2026-10-07
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "029"
down_revision = "028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("source_app", sa.String(50), nullable=True))
    op.add_column("tasks", sa.Column("context", postgresql.JSONB(), nullable=True))
    op.add_column("departments", sa.Column("triage_description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("departments", "triage_description")
    op.drop_column("tasks", "context")
    op.drop_column("tasks", "source_app")
