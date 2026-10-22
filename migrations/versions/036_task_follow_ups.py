"""Add task_follow_ups (seguimientos de una tarea en el calendario)

Revision ID: 036
Revises: 035
Create Date: 2026-10-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "036"
down_revision = "035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_follow_ups",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", UUID(as_uuid=True), sa.ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_at", sa.DateTime, nullable=False),
        sa.Column("end_at", sa.DateTime, nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("reminder_minutes", sa.Integer, nullable=True),
        sa.Column("remind_at", sa.DateTime, nullable=True),
        sa.Column("reminded_at", sa.DateTime, nullable=True),
        sa.Column("done_at", sa.DateTime, nullable=True),
        sa.Column("done_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_by", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_task_follow_ups_task_id", "task_follow_ups", ["task_id"])
    op.create_index("ix_task_follow_ups_start_at", "task_follow_ups", ["start_at"])
    # Solo los recordatorios pendientes: el scheduler los barre cada minuto.
    op.create_index(
        "ix_task_follow_ups_pending_reminder", "task_follow_ups", ["remind_at"],
        postgresql_where=sa.text("reminded_at IS NULL AND remind_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_task_follow_ups_pending_reminder", table_name="task_follow_ups")
    op.drop_index("ix_task_follow_ups_start_at", table_name="task_follow_ups")
    op.drop_index("ix_task_follow_ups_task_id", table_name="task_follow_ups")
    op.drop_table("task_follow_ups")
