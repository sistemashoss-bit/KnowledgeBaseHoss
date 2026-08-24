"""Add global_region_id / global_branch_id (hoss global ids) to zones and branches

Revision ID: 012
Revises: 011
Create Date: 2026-08-24
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "012"
down_revision = "011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # zones.global_region_id  ← hoss Regions.global_region_id (mismo nombre)
    op.add_column(
        "zones",
        sa.Column("global_region_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_unique_constraint("uq_zones_global_region_id", "zones", ["global_region_id"])
    op.create_index("ix_zones_global_region_id", "zones", ["global_region_id"])

    # branches.global_branch_id  ← hoss Branches.global_branch_id (mismo nombre)
    op.add_column(
        "branches",
        sa.Column("global_branch_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_unique_constraint("uq_branches_global_branch_id", "branches", ["global_branch_id"])
    op.create_index("ix_branches_global_branch_id", "branches", ["global_branch_id"])


def downgrade() -> None:
    op.drop_index("ix_branches_global_branch_id", table_name="branches")
    op.drop_constraint("uq_branches_global_branch_id", "branches", type_="unique")
    op.drop_column("branches", "global_branch_id")

    op.drop_index("ix_zones_global_region_id", table_name="zones")
    op.drop_constraint("uq_zones_global_region_id", "zones", type_="unique")
    op.drop_column("zones", "global_region_id")
