"""Add calendar_event_shares (compartir un evento con otras personas)

Revision ID: 038
Revises: 037
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "038"
down_revision = "037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_event_shares",
        sa.Column(
            "event_id", UUID(as_uuid=True), sa.ForeignKey("calendar_events.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_calendar_event_shares_user_id", "calendar_event_shares", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_calendar_event_shares_user_id", table_name="calendar_event_shares")
    op.drop_table("calendar_event_shares")
