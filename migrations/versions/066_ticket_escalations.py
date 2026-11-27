"""Escalamiento de tickets con categoría (PLAN_CATEGORIAS_TICKETS.md, fase 4):
historial ticket_escalations (a mano), ticket_sla_alerts (a quién se avisó que
venció el SLA del paso; también le da permiso de ver el ticket) y
tickets.step_responded_at, la primera respuesta dentro del paso actual (el SLA de
respuesta se mide por paso; first_response_at sigue siendo la del ticket).

Revision ID: 066
Revises: 065
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "066"
down_revision = "065"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tickets", sa.Column("step_responded_at", sa.DateTime(), nullable=True))
    op.create_table(
        "ticket_escalations",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_id", UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_position", sa.Integer(), nullable=True),
        sa.Column("to_position", sa.Integer(), nullable=False),
        sa.Column("from_department_id", UUID(as_uuid=True), sa.ForeignKey("departments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("to_department_id", UUID(as_uuid=True), sa.ForeignKey("departments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("from_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("to_user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("reason", sa.String(20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("actor_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_ticket_escalations_ticket", "ticket_escalations", ["ticket_id"])
    op.create_table(
        "ticket_sla_alerts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("ticket_id", UUID(as_uuid=True), sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("step_position", sa.Integer(), nullable=False),
        sa.Column("metric", sa.String(20), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("ticket_id", "step_position", "metric", "user_id", name="uq_ticket_sla_alerts"),
        sa.CheckConstraint("metric = 'response' OR metric = 'resolution'", name="ck_ticket_sla_alerts_metric"),
    )
    op.create_index("ix_ticket_sla_alerts_user", "ticket_sla_alerts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_ticket_sla_alerts_user", table_name="ticket_sla_alerts")
    op.drop_table("ticket_sla_alerts")
    op.drop_index("ix_ticket_escalations_ticket", table_name="ticket_escalations")
    op.drop_table("ticket_escalations")
    op.drop_column("tickets", "step_responded_at")
