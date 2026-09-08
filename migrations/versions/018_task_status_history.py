"""Add task_status_history (tiempo total por estado)

Revision ID: 018
Revises: 017
Create Date: 2026-09-08
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "018"
down_revision = "017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_status_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("changed_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("changed_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_task_status_history_task_id", "task_status_history", ["task_id"])

    # Backfill: una fila inicial por cada tarea existente (from_status NULL), fechada
    # en su created_at, para que el cálculo de duración funcione también con tareas
    # creadas antes de este cambio.
    op.execute(
        """
        INSERT INTO task_status_history (id, task_id, from_status, to_status, changed_by, changed_at)
        SELECT gen_random_uuid(), id, NULL, status, created_by, created_at FROM tasks
        """
    )


def downgrade() -> None:
    op.drop_index("ix_task_status_history_task_id", "task_status_history")
    op.drop_table("task_status_history")
