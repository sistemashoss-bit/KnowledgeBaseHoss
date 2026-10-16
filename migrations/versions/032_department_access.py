"""Accesos extra a departamentos, alcance por sucursal solo en Comercial

- user_department_access: view/manage sobre otros departamentos.
- departments.branch_scoped: el admin queda acotado a sus sucursales (Comercial).
- users.all_branches: el puesto en hoss cubre todas las sucursales.
- user_zones se retira: las zonas asignadas pasan a sus sucursales en
  user_branches, que desde ahora se sincroniza desde hoss en cada login.

Revision ID: 032
Revises: 031
Create Date: 2026-10-16
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "032"
down_revision = "031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "user_department_access",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("department_id", UUID(as_uuid=True), sa.ForeignKey("departments.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("level", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=True),
        sa.CheckConstraint("level IN ('view', 'manage')", name="ck_user_department_access_level"),
    )
    op.add_column("departments", sa.Column("branch_scoped", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("users", sa.Column("all_branches", sa.Boolean, nullable=False, server_default=sa.false()))

    op.execute("UPDATE departments SET branch_scoped = true WHERE lower(name) = 'comercial'")
    op.execute(
        "INSERT INTO user_branches (user_id, branch_id, assigned_at) "
        "SELECT uz.user_id, b.id, now() FROM user_zones uz JOIN branches b ON b.zone_id = uz.zone_id "
        "ON CONFLICT DO NOTHING"
    )
    # Hasta su próximo login (que trae el dato de hoss), quien no tenía zona ni
    # sucursales conserva el departamento completo, como antes.
    op.execute("UPDATE users SET all_branches = true WHERE id NOT IN (SELECT user_id FROM user_branches)")
    op.drop_table("user_zones")


def downgrade() -> None:
    op.create_table(
        "user_zones",
        sa.Column("user_id", UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("zone_id", UUID(as_uuid=True), sa.ForeignKey("zones.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("assigned_at", sa.DateTime, nullable=True),
    )
    op.drop_column("users", "all_branches")
    op.drop_column("departments", "branch_scoped")
    op.drop_table("user_department_access")
