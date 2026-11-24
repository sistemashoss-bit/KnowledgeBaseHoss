"""Encuestas (app/polls): módulo propio, reutilizable por otras secciones. El
chat las engancha con messages.poll_id.

Revision ID: 063
Revises: 062
Create Date: 2026-11-24
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "063"
down_revision = "062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "polls",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("question", sa.String(300), nullable=False),
        sa.Column("multiple", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "poll_options",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("poll_id", UUID(as_uuid=True), sa.ForeignKey("polls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("text", sa.String(150), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
    )
    op.create_index("ix_poll_options_poll_id", "poll_options", ["poll_id"])
    op.create_table(
        "poll_votes",
        sa.Column("option_id", UUID(as_uuid=True), sa.ForeignKey("poll_options.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("poll_id", UUID(as_uuid=True), sa.ForeignKey("polls.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_poll_votes_poll_user", "poll_votes", ["poll_id", "user_id"])
    op.add_column(
        "messages",
        sa.Column("poll_id", UUID(as_uuid=True), sa.ForeignKey("polls.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_unique_constraint("uq_messages_poll_id", "messages", ["poll_id"])


def downgrade() -> None:
    op.drop_constraint("uq_messages_poll_id", "messages", type_="unique")
    op.drop_column("messages", "poll_id")
    op.drop_index("ix_poll_votes_poll_user", table_name="poll_votes")
    op.drop_table("poll_votes")
    op.drop_index("ix_poll_options_poll_id", table_name="poll_options")
    op.drop_table("poll_options")
    op.drop_table("polls")
