"""Transcripción de audios del chat (Whisper vía OpenRouter): el texto queda en
el adjunto para todos los del chat y no se vuelve a cobrar.

Revision ID: 062
Revises: 061
Create Date: 2026-11-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "062"
down_revision = "061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("message_attachments", sa.Column("transcript", sa.Text(), nullable=True))
    op.add_column("message_attachments", sa.Column("transcribed_at", sa.DateTime(), nullable=True))
    op.add_column(
        "message_attachments",
        sa.Column("transcribed_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("message_attachments", "transcribed_by")
    op.drop_column("message_attachments", "transcribed_at")
    op.drop_column("message_attachments", "transcript")
