"""Tickets: incidencias y solicitudes a un departamento, con folio secuencial,
SLA (primera respuesta y resolución), comentarios con reacciones y evidencias.

Revision ID: 055
Revises: 054
Create Date: 2026-11-07
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "055"
down_revision = "054"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.execute("CREATE SEQUENCE ticket_number_seq START 1")
    op.create_table(
        "tickets",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("number", sa.Integer, nullable=False, unique=True,
                  server_default=sa.text("nextval('ticket_number_seq')")),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("priority", sa.String(10), nullable=False),
        sa.Column("department_id", UUID, sa.ForeignKey("departments.id", ondelete="CASCADE"), nullable=False),
        sa.Column("assigned_to", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("requester_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_app", sa.String(50), nullable=True),
        sa.Column("context", postgresql.JSONB, nullable=True),
        sa.Column("sla_response_minutes", sa.Integer, nullable=True),
        sa.Column("sla_resolution_minutes", sa.Integer, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=True),
        sa.Column("first_response_at", sa.DateTime, nullable=True),
        sa.Column("resolved_at", sa.DateTime, nullable=True),
        sa.Column("closed_at", sa.DateTime, nullable=True),
    )
    op.execute("ALTER SEQUENCE ticket_number_seq OWNED BY tickets.number")
    op.create_index("ix_tickets_department_status", "tickets", ["department_id", "status"])
    op.create_index("ix_tickets_requester", "tickets", ["requester_id"])
    op.create_index("ix_tickets_assigned_to", "tickets", ["assigned_to"])

    op.create_table(
        "ticket_status_history",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("ticket_id", UUID, sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(20), nullable=True),
        sa.Column("to_status", sa.String(20), nullable=False),
        sa.Column("changed_by", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("changed_at", sa.DateTime, nullable=False),
    )
    op.create_index("ix_ticket_status_history_ticket_id", "ticket_status_history", ["ticket_id"])

    op.create_table(
        "ticket_comments",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("ticket_id", UUID, sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_id", UUID, sa.ForeignKey("ticket_comments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("edited", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_ticket_comments_ticket_created", "ticket_comments", ["ticket_id", "created_at"])

    op.create_table(
        "ticket_comment_reactions",
        sa.Column("comment_id", UUID, sa.ForeignKey("ticket_comments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("emoji", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )

    op.create_table(
        "ticket_evidences",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("ticket_id", UUID, sa.ForeignKey("tickets.id", ondelete="CASCADE"), nullable=False),
        sa.Column("uploaded_by", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("file_key", sa.String(500), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("file_size", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_ticket_evidences_ticket_id", "ticket_evidences", ["ticket_id"])

    op.create_table(
        "ticket_sla_policies",
        sa.Column("department_id", UUID, sa.ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("priority", sa.String(10), primary_key=True),
        sa.Column("response_minutes", sa.Integer, nullable=True),
        sa.Column("resolution_minutes", sa.Integer, nullable=True),
        sa.Column("updated_at", sa.DateTime, nullable=True),
    )


def downgrade() -> None:
    op.drop_table("ticket_sla_policies")
    op.drop_table("ticket_evidences")
    op.drop_table("ticket_comment_reactions")
    op.drop_table("ticket_comments")
    op.drop_table("ticket_status_history")
    op.drop_table("tickets")
