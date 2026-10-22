"""Add recurring_task_shares (compartir las tareas que genera una plantilla)

Revision ID: 037
Revises: 036
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "037"
down_revision = "036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recurring_task_shares",
        sa.Column(
            "recurring_task_id", UUID(as_uuid=True),
            sa.ForeignKey("recurring_tasks.id", ondelete="CASCADE"), primary_key=True,
        ),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("recurring_task_shares")
