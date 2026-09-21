"""Add conversations.avatar_key (foto de grupo)

Revision ID: 025
Revises: 024
Create Date: 2026-09-21
"""
import sqlalchemy as sa
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("avatar_key", sa.String(500), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "avatar_key")
