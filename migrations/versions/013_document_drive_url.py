"""Add documents.drive_url and make filename/file_key nullable (Google Drive links)

Revision ID: 013
Revises: 012
Create Date: 2026-09-04
"""
import sqlalchemy as sa
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Link externo de Google Drive (excluyente con file_key).
    op.add_column("documents", sa.Column("drive_url", sa.String(length=1000), nullable=True))
    # Un documento de Drive no tiene archivo en Wasabi → filename/file_key opcionales.
    op.alter_column("documents", "filename", existing_type=sa.String(length=255), nullable=True)
    op.alter_column("documents", "file_key", existing_type=sa.String(length=500), nullable=True)


def downgrade() -> None:
    # Filas de solo-Drive quedarían con file_key null y romperían el NOT NULL;
    # se rellenan con cadena vacía antes de restaurar la restricción.
    op.execute("UPDATE documents SET file_key = '' WHERE file_key IS NULL")
    op.execute("UPDATE documents SET filename = '' WHERE filename IS NULL")
    op.alter_column("documents", "file_key", existing_type=sa.String(length=500), nullable=False)
    op.alter_column("documents", "filename", existing_type=sa.String(length=255), nullable=False)
    op.drop_column("documents", "drive_url")
