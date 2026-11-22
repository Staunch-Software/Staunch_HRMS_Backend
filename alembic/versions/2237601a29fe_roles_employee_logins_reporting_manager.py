"""roles, employee logins and reporting manager

admin_users gains role (existing users become HR), employee_id (link to the
employee for self-service logins), must_change_password and last_login_at.
employees gains reporting_manager_id (self FK) and notice_start_date.

Revision ID: 2237601a29fe
Revises: 908b0564cacd
Create Date: 2026-11-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "2237601a29fe"
down_revision: Union[str, None] = "908b0564cacd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("admin_users", sa.Column("role", sa.String(length=20), server_default=sa.text("'HR'"), nullable=False))
    op.add_column("admin_users", sa.Column("employee_id", sa.Integer(), nullable=True))
    op.add_column(
        "admin_users",
        sa.Column("must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("admin_users", sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True))
    op.create_unique_constraint("uq_admin_users_employee_id", "admin_users", ["employee_id"])
    op.create_foreign_key(
        "fk_admin_users_employee_id_employees", "admin_users", "employees",
        ["employee_id"], ["id"], ondelete="SET NULL",
    )

    op.add_column("employees", sa.Column("notice_start_date", sa.Date(), nullable=True))
    op.add_column("employees", sa.Column("reporting_manager_id", sa.Integer(), nullable=True))
    op.create_index("ix_employees_reporting_manager_id", "employees", ["reporting_manager_id"])
    op.create_foreign_key(
        "fk_employees_reporting_manager_id_employees", "employees", "employees",
        ["reporting_manager_id"], ["id"], ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_employees_reporting_manager_id_employees", "employees", type_="foreignkey")
    op.drop_index("ix_employees_reporting_manager_id", table_name="employees")
    op.drop_column("employees", "reporting_manager_id")
    op.drop_column("employees", "notice_start_date")

    op.drop_constraint("fk_admin_users_employee_id_employees", "admin_users", type_="foreignkey")
    op.drop_constraint("uq_admin_users_employee_id", "admin_users", type_="unique")
    op.drop_column("admin_users", "last_login_at")
    op.drop_column("admin_users", "must_change_password")
    op.drop_column("admin_users", "employee_id")
    op.drop_column("admin_users", "role")
