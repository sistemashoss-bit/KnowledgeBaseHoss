"""Reuniones programadas: eventos con videollamada y confirmación de asistencia

Revision ID: 047
Revises: 046
Create Date: 2026-11-03
"""
import sqlalchemy as sa
from alembic import op

revision = "047"
down_revision = "046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("calendar_events", sa.Column(
        "is_meeting", sa.Boolean(), nullable=False, server_default=sa.false(),
    ))
    # Respuesta del invitado: yes / maybe / no; NULL = sin responder.
    op.add_column("calendar_event_shares", sa.Column("response", sa.String(10), nullable=True))
    op.add_column("calendar_event_shares", sa.Column("responded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("calendar_event_shares", "responded_at")
    op.drop_column("calendar_event_shares", "response")
    op.drop_column("calendar_events", "is_meeting")
