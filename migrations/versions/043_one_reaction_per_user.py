"""Una reacción por persona en cada mensaje/comentario (cambiarla la reemplaza)

Revision ID: 043
Revises: 042
Create Date: 2026-10-28
"""
import sqlalchemy as sa
from alembic import op

revision = "043"
down_revision = "042"
branch_labels = None
depends_on = None

_TABLES = [
    ("message_reactions", "message_id"),
    ("task_comment_reactions", "comment_id"),
    ("calendar_comment_reactions", "comment_id"),
]


def upgrade() -> None:
    for table, column in _TABLES:
        # De quien ya tenía varias se conserva la más reciente.
        op.execute(sa.text(f"""
            DELETE FROM {table} a USING {table} b
            WHERE a.{column} = b.{column} AND a.user_id = b.user_id
              AND (COALESCE(a.created_at, 'epoch'), a.emoji) < (COALESCE(b.created_at, 'epoch'), b.emoji)
        """))
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, [column, "user_id"])


def downgrade() -> None:
    for table, column in _TABLES:
        op.drop_constraint(f"{table}_pkey", table, type_="primary")
        op.create_primary_key(f"{table}_pkey", table, [column, "user_id", "emoji"])
