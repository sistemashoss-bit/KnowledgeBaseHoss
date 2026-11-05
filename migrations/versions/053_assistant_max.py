"""Asistente MAX en el chat: usuario bot, presente en todos los grupos, y
propuestas de acción (tareas/seguimientos) que se confirman desde el mensaje.

El bot se crea inactivo (no sale en selectores ni inicia sesión) y con el
avatar de la mascota del widget (`asset:` lo resuelve storage sin Wasabi).

Revision ID: 053
Revises: 052
Create Date: 2026-11-05
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "053"
down_revision = "052"
branch_labels = None
depends_on = None

# Igual que app.models.ASSISTANT_BOT_ID.
BOT_ID = "00000000-0000-4000-8000-00000000a1a1"


def upgrade() -> None:
    op.add_column("users", sa.Column("is_bot", sa.Boolean(), nullable=False, server_default=sa.text("false")))

    op.execute(f"""
        INSERT INTO users (id, email, name, role, avatar_key, all_branches, is_active, is_bot, created_at, updated_at)
        VALUES ('{BOT_ID}', 'max@asistente.knowledge', 'MAX', 'employee', 'asset:mascota.webp',
                false, false, true, now(), now())
        ON CONFLICT (id) DO NOTHING
    """)
    op.execute(f"""
        INSERT INTO conversation_participants (conversation_id, user_id, joined_at)
        SELECT c.id, '{BOT_ID}', now() FROM conversations c
        WHERE c.type = 'group'
        ON CONFLICT DO NOTHING
    """)

    op.create_table(
        "assistant_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("messages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("invoker_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("error", sa.String(300), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("assistant_proposals")
    op.execute(f"DELETE FROM users WHERE id = '{BOT_ID}'")
    op.drop_column("users", "is_bot")
