"""El editor de documentos pasa a Herramientas como utilidad sin guardado
propio (solo sube el PDF a Documentos): se quita documents.editor_project.

Revision ID: 058
Revises: 057
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

revision = "058"
down_revision = "057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("documents", "editor_project")


def downgrade() -> None:
    op.add_column("documents", sa.Column("editor_project", sa.Text(), nullable=True))
