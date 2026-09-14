"""Add documents.is_work_document (documento de trabajo vs. archivo común)

Revision ID: 022
Revises: 021
Create Date: 2026-09-14
"""
import sqlalchemy as sa
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("is_work_document", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("documents", "is_work_document")
