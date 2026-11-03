"""Pizarras colaborativas (Excalidraw + Yjs)

Revision ID: 048
Revises: 047
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "048"
down_revision = "047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "whiteboards",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("department_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("departments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("ydoc", sa.LargeBinary(), nullable=True),
        sa.Column("last_edited_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_whiteboards_owner_id", "whiteboards", ["owner_id"])
    op.create_index(
        "ix_whiteboards_department_id", "whiteboards", ["department_id"],
        postgresql_where=sa.text("department_id IS NOT NULL"),
    )
    op.create_table(
        "whiteboard_shares",
        sa.Column("board_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("whiteboards.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("can_edit", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_whiteboard_shares_user_id", "whiteboard_shares", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_whiteboard_shares_user_id", table_name="whiteboard_shares")
    op.drop_table("whiteboard_shares")
    op.drop_index("ix_whiteboards_department_id", table_name="whiteboards")
    op.drop_index("ix_whiteboards_owner_id", table_name="whiteboards")
    op.drop_table("whiteboards")
