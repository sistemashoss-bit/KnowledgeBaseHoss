"""Gantt en proyectos: projects.gantt_enabled lo activa por proyecto,
tasks.start_date es el inicio opcional (si falta, el Gantt usa created_at) y
task_dependencies guarda qué tarea espera a cuál.

Revision ID: 061
Revises: 060
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "061"
down_revision = "060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("gantt_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("tasks", sa.Column("start_date", sa.Date(), nullable=True))
    op.create_table(
        "task_dependencies",
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("depends_on_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.CheckConstraint("task_id <> depends_on_id", name="ck_task_dependencies_not_self"),
    )
    op.create_index("ix_task_dependencies_depends_on_id", "task_dependencies", ["depends_on_id"])


def downgrade() -> None:
    op.drop_index("ix_task_dependencies_depends_on_id", table_name="task_dependencies")
    op.drop_table("task_dependencies")
    op.drop_column("tasks", "start_date")
    op.drop_column("projects", "gantt_enabled")
