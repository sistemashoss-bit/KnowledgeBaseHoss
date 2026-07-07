"""Add avatar_key to users

Revision ID: 002
Revises: 001
Create Date: 2026-07-07
"""
import sqlalchemy as sa
from alembic import op

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("avatar_key", sa.String(500), nullable=True))


def downgrade():
    op.drop_column("users", "avatar_key")
