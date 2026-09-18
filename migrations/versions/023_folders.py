"""Add folders + folder_allowed_users, and documents.folder_id

Revision ID: 023
Revises: 022
Create Date: 2026-09-18
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "023"
down_revision = "022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "folders",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(150), nullable=False),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", UUID(as_uuid=True), sa.ForeignKey("folders.id", ondelete="CASCADE"), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("owner_id", "parent_id", "name", name="uq_folders_owner_parent_name"),
    )
    op.create_index("ix_folders_owner_id", "folders", ["owner_id"])
    op.create_index("ix_folders_parent_id", "folders", ["parent_id"])

    op.create_table(
        "folder_allowed_users",
        sa.Column("folder_id", UUID(as_uuid=True), sa.ForeignKey("folders.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("added_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_folder_allowed_users_user_id", "folder_allowed_users", ["user_id"])

    op.add_column(
        "documents",
        sa.Column("folder_id", UUID(as_uuid=True), sa.ForeignKey("folders.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_documents_folder_id", "documents", ["folder_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_folder_id", table_name="documents")
    op.drop_column("documents", "folder_id")

    op.drop_index("ix_folder_allowed_users_user_id", table_name="folder_allowed_users")
    op.drop_table("folder_allowed_users")

    op.drop_index("ix_folders_parent_id", table_name="folders")
    op.drop_index("ix_folders_owner_id", table_name="folders")
    op.drop_table("folders")
