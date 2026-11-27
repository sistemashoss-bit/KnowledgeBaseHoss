"""Categorías de tickets con cadena de pasos (PLAN_CATEGORIAS_TICKETS.md, fase 3):
ticket_categories (del departamento de quien reporta, hoy Comercial),
ticket_category_steps (jefe directo / departamento / persona, con su SLA) y en
tickets la categoría, el paso actual y cuándo empezó.

Revision ID: 065
Revises: 064
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "065"
down_revision = "064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ticket_categories",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("department_id", UUID(as_uuid=True), sa.ForeignKey("departments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("default_kind", sa.String(20), nullable=False, server_default="incident"),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("department_id", "name", name="uq_ticket_categories_department_name"),
    )
    op.create_table(
        "ticket_category_steps",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("category_id", UUID(as_uuid=True), sa.ForeignKey("ticket_categories.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("target", sa.String(20), nullable=False),
        sa.Column("department_id", UUID(as_uuid=True), sa.ForeignKey("departments.id", ondelete="CASCADE"), nullable=True),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=True),
        sa.Column("sla_response_minutes", sa.Integer(), nullable=True),
        sa.Column("sla_resolution_minutes", sa.Integer(), nullable=True),
        sa.Column("alert_target", sa.String(20), nullable=True),
        sa.Column("alert_department_id", UUID(as_uuid=True), sa.ForeignKey("departments.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("category_id", "position", name="uq_ticket_category_steps_position"),
        sa.CheckConstraint(
            "alert_target IS NULL OR alert_target = 'boss' OR alert_target = 'department'",
            name="ck_ticket_category_steps_alert",
        ),
        sa.CheckConstraint(
            "(target = 'direct_boss' AND department_id IS NULL AND user_id IS NULL)"
            " OR (target = 'department' AND department_id IS NOT NULL AND user_id IS NULL)"
            " OR (target = 'user' AND user_id IS NOT NULL AND department_id IS NULL)",
            name="ck_ticket_category_steps_target",
        ),
    )
    op.add_column(
        "tickets",
        sa.Column("category_id", UUID(as_uuid=True), sa.ForeignKey("ticket_categories.id", ondelete="SET NULL"), nullable=True),
    )
    op.add_column("tickets", sa.Column("step_position", sa.Integer(), nullable=True))
    op.add_column("tickets", sa.Column("step_started_at", sa.DateTime(), nullable=True))
    op.create_index("ix_tickets_category", "tickets", ["category_id"])


def downgrade() -> None:
    op.drop_index("ix_tickets_category", table_name="tickets")
    op.drop_column("tickets", "step_started_at")
    op.drop_column("tickets", "step_position")
    op.drop_column("tickets", "category_id")
    op.drop_table("ticket_category_steps")
    op.drop_table("ticket_categories")
