"""Add repetición a calendar_events y calendar_event_comments

Revision ID: 034
Revises: 033
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "034"
down_revision = "033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calendar_events", sa.Column("frequency", sa.String(10), nullable=True))
    op.add_column("calendar_events", sa.Column("day_of_week", sa.Integer, nullable=True))
    op.add_column("calendar_events", sa.Column("day_of_month", sa.Integer, nullable=True))
    op.add_column("calendar_events", sa.Column("days_of_week", sa.String(20), nullable=True))
    op.add_column("calendar_events", sa.Column("repeat_until", sa.Date, nullable=True))

    op.create_table(
        "calendar_event_comments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id", UUID(as_uuid=True),
            sa.ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_calendar_event_comments_event_id", "calendar_event_comments", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_calendar_event_comments_event_id", table_name="calendar_event_comments")
    op.drop_table("calendar_event_comments")
    op.drop_column("calendar_events", "repeat_until")
    op.drop_column("calendar_events", "days_of_week")
    op.drop_column("calendar_events", "day_of_month")
    op.drop_column("calendar_events", "day_of_week")
    op.drop_column("calendar_events", "frequency")
