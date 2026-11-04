"""Capacitación: visibilidad por departamento y sin "público"

Las capacitaciones públicas pasan a "employee" (toda la empresa, con sesión).
department_id deja de ser obligatorio: un superadmin sin departamento puede
subir con visibilidad de empresa o de personas específicas.

Revision ID: 052
Revises: 051
Create Date: 2026-11-04
"""
from alembic import op

revision = "052"
down_revision = "051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE trainings SET status = 'employee' WHERE status = 'public'")
    op.alter_column("trainings", "department_id", nullable=True)


def downgrade() -> None:
    op.execute("UPDATE trainings SET status = 'employee' WHERE status = 'department'")
    op.execute("DELETE FROM trainings WHERE department_id IS NULL")
    op.alter_column("trainings", "department_id", nullable=False)
