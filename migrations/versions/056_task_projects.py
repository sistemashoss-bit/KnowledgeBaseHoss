"""Tareas en varios proyectos: tabla task_projects (muchos a muchos) en lugar de
tasks.project_id. Copia los vínculos actuales antes de quitar la columna. Las
plantillas recurrentes siguen con un solo proyecto (recurring_tasks.project_id).

Revision ID: 056
Revises: 055
Create Date: 2026-11-09
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "056"
down_revision = "055"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "task_projects",
        sa.Column("task_id", UUID, sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_index("ix_task_projects_project_id", "task_projects", ["project_id"])
    op.execute(
        "INSERT INTO task_projects (task_id, project_id) "
        "SELECT id, project_id FROM tasks WHERE project_id IS NOT NULL"
    )
    op.execute("DROP INDEX IF EXISTS ix_tasks_project_id")
    op.drop_column("tasks", "project_id")


def downgrade() -> None:
    # Vuelve a un proyecto por tarea: si tenía varios, se queda con uno.
    op.add_column(
        "tasks",
        sa.Column("project_id", UUID, sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=True),
    )
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.execute(
        "UPDATE tasks SET project_id = tp.project_id FROM ("
        "SELECT DISTINCT ON (task_id) task_id, project_id FROM task_projects ORDER BY task_id, project_id"
        ") tp WHERE tp.task_id = tasks.id"
    )
    op.drop_index("ix_task_projects_project_id", table_name="task_projects")
    op.drop_table("task_projects")
