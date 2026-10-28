"""Add índices de tareas/comentarios y quita índices duplicados de columnas únicas

Revision ID: 042
Revises: 041
Create Date: 2026-10-28
"""
import sqlalchemy as sa
from alembic import op

revision = "042"
down_revision = "041"
branch_labels = None
depends_on = None

# Índices simples que repiten el de su restricción UNIQUE (mismo btree sobre la
# misma columna): solo cuestan escrituras y espacio.
_DUPLICATES = [
    ("ix_users_email", "users", "email"),
    ("ix_users_corporate_id", "users", "corporate_id"),
    ("ix_zones_global_region_id", "zones", "global_region_id"),
    ("ix_branches_global_branch_id", "branches", "global_branch_id"),
    ("ix_departments_global_department_id", "departments", "global_department_id"),
]


def upgrade() -> None:
    # Detalle de tarea: sus comentarios en orden.
    op.create_index("ix_task_comments_task_created", "task_comments", ["task_id", "created_at"])
    # El alcance de tareas es `creador OR asignado OR departamento`: con una sola
    # rama sin índice, Postgres recorre la tabla completa.
    op.create_index("ix_tasks_created_by", "tasks", ["created_by"])
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])

    conn = op.get_bind()
    for name, _, _ in _DUPLICATES:
        indexdef = conn.execute(
            sa.text("SELECT indexdef FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :n"),
            {"n": name},
        ).scalar()
        # Solo si es el duplicado NO único: si en alguna base ese nombre es el
        # índice único de la columna, borrarlo quitaría la restricción.
        if indexdef and "UNIQUE" not in indexdef.upper():
            op.drop_index(name, table_name=None)


def downgrade() -> None:
    for name, table, column in _DUPLICATES:
        op.create_index(name, table, [column], if_not_exists=True)
    op.drop_index("ix_tasks_project_id", table_name="tasks")
    op.drop_index("ix_tasks_created_by", table_name="tasks")
    op.drop_index("ix_task_comments_task_created", table_name="task_comments")
