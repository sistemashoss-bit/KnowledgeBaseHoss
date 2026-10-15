"""Add calendar_events + calendar_subscriptions, tasks.due_reminded_on

Revision ID: 031
Revises: 030
Create Date: 2026-10-14
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "031"
down_revision = "030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("color", sa.String(20), nullable=False, server_default="blue"),
        sa.Column("all_day", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("start_at", sa.DateTime, nullable=False),
        sa.Column("end_at", sa.DateTime, nullable=False),
        sa.Column("reminder_minutes", sa.Integer, nullable=True),
        sa.Column("remind_at", sa.DateTime, nullable=True),
        sa.Column("reminded_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_calendar_events_owner_start", "calendar_events", ["owner_id", "start_at"])
    # Solo los recordatorios pendientes: el scheduler los barre cada minuto.
    op.create_index(
        "ix_calendar_events_pending_reminder", "calendar_events", ["remind_at"],
        postgresql_where=sa.text("reminded_at IS NULL AND remind_at IS NOT NULL"),
    )

    op.create_table(
        "calendar_subscriptions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("viewer_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("target_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column(
            "target_department_id", UUID(as_uuid=True),
            sa.ForeignKey("departments.id", ondelete="CASCADE"), nullable=True,
        ),
        sa.Column("is_visible", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.UniqueConstraint("viewer_id", "target_user_id", name="uq_calendar_subscriptions_viewer_user"),
        sa.UniqueConstraint("viewer_id", "target_department_id", name="uq_calendar_subscriptions_viewer_department"),
        sa.CheckConstraint(
            "(target_user_id IS NULL) <> (target_department_id IS NULL)",
            name="ck_calendar_subscriptions_one_target",
        ),
    )
    op.create_index("ix_calendar_subscriptions_viewer_id", "calendar_subscriptions", ["viewer_id"])

    op.add_column("tasks", sa.Column("due_reminded_on", sa.Date, nullable=True))
    op.create_index("ix_tasks_due_date", "tasks", ["due_date"])


def downgrade() -> None:
    op.drop_index("ix_tasks_due_date", table_name="tasks")
    op.drop_column("tasks", "due_reminded_on")
    op.drop_index("ix_calendar_subscriptions_viewer_id", table_name="calendar_subscriptions")
    op.drop_table("calendar_subscriptions")
    op.drop_index("ix_calendar_events_pending_reminder", table_name="calendar_events")
    op.drop_index("ix_calendar_events_owner_start", table_name="calendar_events")
    op.drop_table("calendar_events")
