"""Documentos hechos en el editor (GrapesJS): documents.editor_project guarda el
proyecto para volver a editarlo y regenerar su PDF.

Revision ID: 057
Revises: 056
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

revision = "057"
down_revision = "056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("editor_project", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "editor_project")
