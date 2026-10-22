"""Add calendar_event_comments.edited

Revision ID: 035
Revises: 034
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op

revision = "035"
down_revision = "034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "calendar_event_comments",
        sa.Column("edited", sa.Boolean, nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("calendar_event_comments", "edited")
