"""Add departments.global_department_id (hoss global id)

Revision ID: 026
Revises: 025
Create Date: 2026-10-02
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "026"
down_revision = "025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # departments.global_department_id  ← hoss Departments.global_department_id (mismo nombre)
    op.add_column(
        "departments",
        sa.Column("global_department_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_departments_global_department_id", "departments", ["global_department_id"]
    )
    op.create_index("ix_departments_global_department_id", "departments", ["global_department_id"])


def downgrade() -> None:
    op.drop_index("ix_departments_global_department_id", table_name="departments")
    op.drop_constraint("uq_departments_global_department_id", "departments", type_="unique")
    op.drop_column("departments", "global_department_id")
