"""Add documents.content_html for in-app (TipTap) authored documents

Revision ID: 009
Revises: 008
Create Date: 2026-08-17
"""
import sqlalchemy as sa
from alembic import op

revision = "009"
down_revision = "008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("content_html", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "content_html")
