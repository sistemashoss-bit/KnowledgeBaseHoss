"""Add users.position_name (puesto en hoss, solo informativo)

Revision ID: 027
Revises: 026
Create Date: 2026-10-02
"""
import sqlalchemy as sa
from alembic import op

revision = "027"
down_revision = "026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("position_name", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "position_name")
