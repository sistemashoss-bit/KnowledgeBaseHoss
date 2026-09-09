"""Add task_tags + task_tag_assignments (etiquetas por departamento)

Revision ID: 020
Revises: 019
Create Date: 2026-09-09
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "020"
down_revision = "019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(50), nullable=False),
        sa.Column("color", sa.String(20), nullable=False, server_default="gray"),
        sa.Column("department_id", UUID(as_uuid=True), sa.ForeignKey("departments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("department_id", "name", name="uq_task_tags_department_name"),
    )
    op.create_index("ix_task_tags_department_id", "task_tags", ["department_id"])

    op.create_table(
        "task_tag_assignments",
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("tag_id", UUID(as_uuid=True), sa.ForeignKey("task_tags.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("added_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_task_tag_assignments_tag_id", "task_tag_assignments", ["tag_id"])


def downgrade() -> None:
    op.drop_index("ix_task_tag_assignments_tag_id", table_name="task_tag_assignments")
    op.drop_table("task_tag_assignments")
    op.drop_index("ix_task_tags_department_id", table_name="task_tags")
    op.drop_table("task_tags")
