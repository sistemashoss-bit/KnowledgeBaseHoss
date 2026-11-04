"""Capacitación: transcripción (subtítulos de Mux) y capítulos (Mux Robots)

Revision ID: 051
Revises: 050
Create Date: 2026-11-04
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "051"
down_revision = "050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trainings", sa.Column("captions_status", sa.String(20), nullable=True))
    op.add_column("trainings", sa.Column("captions_track_id", sa.String(100), nullable=True))
    op.add_column("trainings", sa.Column("transcript", sa.Text(), nullable=True))
    op.add_column("trainings", sa.Column("chapters_status", sa.String(20), nullable=True))
    op.add_column("trainings", sa.Column("chapters_job_id", sa.String(100), nullable=True))
    op.add_column("trainings", sa.Column("chapters", postgresql.JSONB(), nullable=True))
    op.create_index("ix_trainings_chapters_job_id", "trainings", ["chapters_job_id"])


def downgrade() -> None:
    op.drop_index("ix_trainings_chapters_job_id", table_name="trainings")
    for col in ("chapters", "chapters_job_id", "chapters_status", "transcript", "captions_track_id", "captions_status"):
        op.drop_column("trainings", col)
