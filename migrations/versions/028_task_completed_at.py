"""Add tasks.completed_at (cuándo pasó a Listo) y rellena las tareas ya en Listo

Revision ID: 028
Revises: 027
Create Date: 2026-10-05
"""
import sqlalchemy as sa
from alembic import op

revision = "028"
down_revision = "027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("completed_at", sa.DateTime(), nullable=True))
    op.execute(
        """
        UPDATE tasks t
        SET completed_at = COALESCE(
            (SELECT MAX(h.changed_at) FROM task_status_history h
             WHERE h.task_id = t.id AND h.to_status = 'done'),
            t.updated_at
        )
        WHERE t.status = 'done'
        """
    )


def downgrade() -> None:
    op.drop_column("tasks", "completed_at")
