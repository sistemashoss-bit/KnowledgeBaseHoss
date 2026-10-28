"""Add reactions (mensajes, comentarios de tareas y de calendario) e índices del chat

Revision ID: 041
Revises: 040
Create Date: 2026-10-28
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "041"
down_revision = "040"
branch_labels = None
depends_on = None

# (tabla, columna objetivo, tabla objetivo). La PK (objetivo, user, emoji) sirve
# de índice para el `WHERE <objetivo> IN (...)` con que se leen.
_REACTION_TABLES = [
    ("message_reactions", "message_id", "messages"),
    ("task_comment_reactions", "comment_id", "task_comments"),
    ("calendar_comment_reactions", "comment_id", "calendar_event_comments"),
]


def upgrade() -> None:
    for table, column, target in _REACTION_TABLES:
        op.create_table(
            table,
            sa.Column(column, UUID(as_uuid=True), sa.ForeignKey(f"{target}.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
            sa.Column("emoji", sa.String(16), primary_key=True),
            sa.Column("created_at", sa.DateTime, nullable=True),
        )

    # El chat siempre filtra por conversación y ordena por fecha (feed, último
    # mensaje, no leídos): el compuesto cubre también lo que hacía el simple.
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])
    op.drop_index("ix_messages_conversation_id", table_name="messages")
    # La PK empieza por conversation_id; "mis conversaciones" busca por user_id.
    op.create_index("ix_conversation_participants_user_id", "conversation_participants", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_participants_user_id", table_name="conversation_participants")
    op.create_index("ix_messages_conversation_id", "messages", ["conversation_id"])
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    for table, _, _ in reversed(_REACTION_TABLES):
        op.drop_table(table)
