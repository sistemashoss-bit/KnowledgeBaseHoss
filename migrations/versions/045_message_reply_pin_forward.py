"""Responder, fijar y reenviar mensajes del chat

Revision ID: 045
Revises: 044
Create Date: 2026-10-30
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "045"
down_revision = "044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("messages", sa.Column(
        "reply_to_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True,
    ))
    op.add_column("messages", sa.Column("pinned_at", sa.DateTime(), nullable=True))
    op.add_column("messages", sa.Column(
        "pinned_by", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    ))
    op.add_column("messages", sa.Column("forwarded", sa.Boolean(), nullable=False, server_default=sa.false()))
    # La purga diaria borra mensajes y el SET NULL busca a quién los citaba: sin
    # este índice recorrería `messages` completa por cada fila borrada.
    op.create_index(
        "ix_messages_reply_to_id", "messages", ["reply_to_id"],
        postgresql_where=sa.text("reply_to_id IS NOT NULL"),
    )
    # Barra de fijados: solo contiene los fijados (pocos por chat).
    op.create_index(
        "ix_messages_conversation_pinned", "messages", ["conversation_id"],
        postgresql_where=sa.text("pinned_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_messages_conversation_pinned", table_name="messages")
    op.drop_index("ix_messages_reply_to_id", table_name="messages")
    op.drop_column("messages", "forwarded")
    op.drop_column("messages", "pinned_by")
    op.drop_column("messages", "pinned_at")
    op.drop_column("messages", "reply_to_id")
