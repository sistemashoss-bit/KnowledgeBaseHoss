"""Add tasks.document_id and recurring_tasks.document_id (referencia a un Document)

Revision ID: 014
Revises: 013
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "014"
down_revision = "013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Documento a revisar/corregir (subido o link de Drive), opcional.
    op.add_column("tasks", sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_tasks_document_id", "tasks", "documents",
        ["document_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_tasks_document_id", "tasks", ["document_id"])

    op.add_column("recurring_tasks", sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_recurring_tasks_document_id", "recurring_tasks", "documents",
        ["document_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_recurring_tasks_document_id", "recurring_tasks", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_recurring_tasks_document_id", table_name="recurring_tasks")
    op.drop_constraint("fk_recurring_tasks_document_id", "recurring_tasks", type_="foreignkey")
    op.drop_column("recurring_tasks", "document_id")

    op.drop_index("ix_tasks_document_id", table_name="tasks")
    op.drop_constraint("fk_tasks_document_id", "tasks", type_="foreignkey")
    op.drop_column("tasks", "document_id")
