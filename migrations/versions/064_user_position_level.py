"""Nivel del puesto en hoss-api (Roles.level), copiado en cada login. Sirve para
encontrar al jefe directo en Comercial (PLAN_CATEGORIAS_TICKETS.md); los permisos
siguen saliendo de users.role.

Revision ID: 064
Revises: 063
Create Date: 2026-11-27
"""
import sqlalchemy as sa
from alembic import op

revision = "064"
down_revision = "063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("position_level", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "position_level")
