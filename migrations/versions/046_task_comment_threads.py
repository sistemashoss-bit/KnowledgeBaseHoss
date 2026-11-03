"""Hilos en los comentarios de tareas: respuestas a un comentario raíz

Revision ID: 046
Revises: 045
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Un solo nivel: parent_id siempre apunta a un comentario raíz. Si se borra
    # el raíz, sus respuestas quedan como comentarios sueltos (SET NULL).
    op.add_column("task_comments", sa.Column(
        "parent_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("task_comments.id", ondelete="SET NULL"), nullable=True,
    ))
    # El SET NULL al borrar busca las respuestas del comentario: sin índice
    # recorrería task_comments completa.
    op.create_index(
        "ix_task_comments_parent_id", "task_comments", ["parent_id"],
        postgresql_where=sa.text("parent_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_task_comments_parent_id", table_name="task_comments")
    op.drop_column("task_comments", "parent_id")
