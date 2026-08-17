"""Recreate all foreign keys with ON DELETE CASCADE

So a plain DELETE (e.g. removing a user) cascades to every dependent row
instead of failing with a foreign key violation.

Revision ID: 008
Revises: 007
Create Date: 2026-08-17
"""
import sqlalchemy as sa
from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


# (table, column, referenced_table) for every FK in the schema.
FKS = [
    ("branches", "zone_id", "zones"),
    ("departments", "branch_id", "branches"),
    ("user_zones", "user_id", "users"),
    ("user_zones", "zone_id", "zones"),
    ("users", "department_id", "departments"),
    ("users", "branch_id", "branches"),
    ("documents", "department_id", "departments"),
    ("documents", "uploaded_by", "users"),
    ("projects", "department_id", "departments"),
    ("projects", "branch_id", "branches"),
    ("projects", "zone_id", "zones"),
    ("projects", "created_by", "users"),
    ("tasks", "project_id", "projects"),
    ("tasks", "department_id", "departments"),
    ("tasks", "assigned_to", "users"),
    ("tasks", "created_by", "users"),
    ("task_comments", "task_id", "tasks"),
    ("task_comments", "user_id", "users"),
    ("task_evidences", "task_id", "tasks"),
    ("task_evidences", "uploaded_by", "users"),
    ("conversations", "zone_id", "zones"),
    ("conversations", "branch_id", "branches"),
    ("conversations", "department_id", "departments"),
    ("conversations", "created_by", "users"),
    ("conversation_participants", "conversation_id", "conversations"),
    ("conversation_participants", "user_id", "users"),
    ("messages", "conversation_id", "conversations"),
    ("messages", "sender_id", "users"),
    ("message_attachments", "message_id", "messages"),
    ("message_attachments", "uploaded_by", "users"),
]


def _existing_fk_name(bind, table: str, column: str) -> str | None:
    """Return the current FK constraint name for (table, column), if any."""
    return bind.execute(
        sa.text(
            """
            SELECT tc.constraint_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_name = :table
              AND kcu.column_name = :column
            LIMIT 1
            """
        ),
        {"table": table, "column": column},
    ).scalar()


def _rebuild(ondelete: str | None) -> None:
    bind = op.get_bind()
    for table, column, ref_table in FKS:
        old_name = _existing_fk_name(bind, table, column)
        if old_name:
            op.drop_constraint(old_name, table, type_="foreignkey")
        op.create_foreign_key(
            f"fk_{table}_{column}",
            table,
            ref_table,
            [column],
            ["id"],
            ondelete=ondelete,
        )


def upgrade() -> None:
    _rebuild("CASCADE")


def downgrade() -> None:
    _rebuild(None)
