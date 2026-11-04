"""Capacitación: videos en Mux con el mismo alcance que los documentos

Revision ID: 050
Revises: 049
Create Date: 2026-11-04
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "050"
down_revision = "049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trainings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("department_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("departments.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("uploaded_by", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("mux_upload_id", sa.String(100), nullable=True, unique=True),
        sa.Column("mux_asset_id", sa.String(100), nullable=True, unique=True),
        sa.Column("mux_playback_id", sa.String(100), nullable=True),
        sa.Column("playback_policy", sa.String(10), nullable=False, server_default="signed"),
        sa.Column("video_status", sa.String(20), nullable=False, server_default="uploading"),
        sa.Column("duration", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "training_allowed_users",
        sa.Column("training_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("trainings.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True),
        sa.Column("added_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("training_allowed_users")
    op.drop_table("trainings")
