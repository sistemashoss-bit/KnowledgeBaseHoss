"""Add user_branches (supervisores: sucursales sueltas sin cubrir la zona)

Revision ID: 024
Revises: 023
Create Date: 2026-09-21
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "024"
down_revision = "023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_branches",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("branch_id", UUID(as_uuid=True), sa.ForeignKey("branches.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("assigned_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("user_branches")
