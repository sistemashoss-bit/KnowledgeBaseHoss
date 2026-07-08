"""Add zones, branches, user_zones, projects, tasks, conversations, messages

Revision ID: 004
Revises: 003
Create Date: 2026-07-08
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = "004"
down_revision = "003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── zones ─────────────────────────────────────────────────────────────────
    op.create_table(
        "zones",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )

    # ── branches ──────────────────────────────────────────────────────────────
    op.create_table(
        "branches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False, unique=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("zones.id"), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )

    # ── modify departments: add branch_id ──────────────────────────────────────
    op.add_column("departments", sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_departments_branch_id", "departments", "branches", ["branch_id"], ["id"])

    # ── modify users: add name + branch_id ────────────────────────────────────
    op.add_column("users", sa.Column("name", sa.String(150), nullable=True))
    op.add_column("users", sa.Column("branch_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_users_branch_id", "users", "branches", ["branch_id"], ["id"])

    # ── user_zones (junction) ─────────────────────────────────────────────────
    op.create_table(
        "user_zones",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("zones.id"), primary_key=True),
        sa.Column("assigned_at", sa.DateTime, nullable=True),
    )

    # ── projects ──────────────────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id"), nullable=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("zones.id"), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("start_date", sa.Date, nullable=True),
        sa.Column("end_date", sa.Date, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )

    # ── tasks ─────────────────────────────────────────────────────────────────
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("priority", sa.String(10), nullable=False, server_default="medium"),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("due_date", sa.Date, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_tasks_department_id", "tasks", ["department_id"])
    op.create_index("ix_tasks_assigned_to", "tasks", ["assigned_to"])
    op.create_index("ix_tasks_status", "tasks", ["status"])

    # ── task_comments ─────────────────────────────────────────────────────────
    op.create_table(
        "task_comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )

    # ── conversations ─────────────────────────────────────────────────────────
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", sa.String(10), nullable=False, server_default="direct"),
        sa.Column("name", sa.String(150), nullable=True),
        sa.Column("zone_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("zones.id"), nullable=True),
        sa.Column("branch_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("branches.id"), nullable=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )

    # ── conversation_participants ──────────────────────────────────────────────
    op.create_table(
        "conversation_participants",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), primary_key=True),
        sa.Column("last_read_at", sa.DateTime, nullable=True),
        sa.Column("joined_at", sa.DateTime, nullable=True),
    )

    # ── messages ──────────────────────────────────────────────────────────────
    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id"), nullable=False, index=True),
        sa.Column("sender_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True, index=True),
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversation_participants")
    op.drop_table("conversations")
    op.drop_table("task_comments")
    op.drop_index("ix_tasks_status", "tasks")
    op.drop_index("ix_tasks_assigned_to", "tasks")
    op.drop_index("ix_tasks_department_id", "tasks")
    op.drop_table("tasks")
    op.drop_table("projects")
    op.drop_table("user_zones")
    op.drop_constraint("fk_users_branch_id", "users", type_="foreignkey")
    op.drop_column("users", "branch_id")
    op.drop_column("users", "name")
    op.drop_constraint("fk_departments_branch_id", "departments", type_="foreignkey")
    op.drop_column("departments", "branch_id")
    op.drop_table("branches")
    op.drop_table("zones")
