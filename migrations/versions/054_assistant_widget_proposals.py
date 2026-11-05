"""Propuestas de MAX desde el widget: sin mensaje ni conversación.

`message_id` y `conversation_id` pasan a opcionales; una propuesta sin
conversación es del widget (privada, solo la ve y confirma quien preguntó).

Revision ID: 054
Revises: 053
Create Date: 2026-11-05
"""
from alembic import op

revision = "054"
down_revision = "053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("assistant_proposals", "message_id", nullable=True)
    op.alter_column("assistant_proposals", "conversation_id", nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM assistant_proposals WHERE message_id IS NULL OR conversation_id IS NULL")
    op.alter_column("assistant_proposals", "conversation_id", nullable=False)
    op.alter_column("assistant_proposals", "message_id", nullable=False)
