"""Comunicados: documents.kind distingue un documento normal de un comunicado
(misma tabla y permisos; el comunicado solo puede ser PDF).

Revision ID: 059
Revises: 058
Create Date: 2026-11-10
"""
import sqlalchemy as sa
from alembic import op

revision = "059"
down_revision = "058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("kind", sa.String(20), nullable=False, server_default="document"),
    )
    op.create_index("ix_documents_kind", "documents", ["kind"])


def downgrade() -> None:
    op.drop_index("ix_documents_kind", table_name="documents")
    op.drop_column("documents", "kind")
