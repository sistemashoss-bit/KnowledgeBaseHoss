"""Full SSO with hoss-api: add corporate_id, drop local credential/MFA columns

Revision ID: 007
Revises: 006
Create Date: 2026-08-17
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "007"
down_revision = "006"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "users",
        sa.Column("corporate_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_unique_constraint("uq_users_corporate_id", "users", ["corporate_id"])
    op.create_index("ix_users_corporate_id", "users", ["corporate_id"])

    # Credenciales y MFA ahora viven en hoss-api (full SSO).
    op.drop_column("users", "password_hash")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "totp_enabled")


def downgrade():
    op.add_column("users", sa.Column("totp_enabled", sa.Boolean(), nullable=True))
    op.add_column("users", sa.Column("totp_secret", sa.String(255), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.String(255), nullable=True))
    op.drop_index("ix_users_corporate_id", table_name="users")
    op.drop_constraint("uq_users_corporate_id", "users", type_="unique")
    op.drop_column("users", "corporate_id")
