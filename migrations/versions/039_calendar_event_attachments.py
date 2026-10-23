"""Add calendar_event_attachments (archivos/evidencias en eventos)

Revision ID: 039
Revises: 038
Create Date: 2026-10-23
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "039"
down_revision = "038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_event_attachments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id", UUID(as_uuid=True), sa.ForeignKey("calendar_events.id", ondelete="CASCADE"), nullable=False,
        ),
        sa.Column("uploaded_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_key", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("file_size", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_calendar_event_attachments_event_id", "calendar_event_attachments", ["event_id"])


def downgrade() -> None:
    op.drop_index("ix_calendar_event_attachments_event_id", table_name="calendar_event_attachments")
    op.drop_table("calendar_event_attachments")
