"""WFH requests, department managers and late blocks

Adds the wfh_requests table, departments.manager_employee_id (approver for
leave, permission and WFH requests), and the attendance policy settings
late_block_size and wfh_min_notice_days. Hand-written.

Revision ID: 8e7ff8df9594
Revises: 4cecedcccfb1
Create Date: 2026-11-17 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8e7ff8df9594'
down_revision: Union[str, None] = '4cecedcccfb1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _admin_fk():
    return sa.ForeignKey("admin_users.id", ondelete="SET NULL")


def upgrade() -> None:
    op.add_column("attendance_policies",
                  sa.Column("late_block_size", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("attendance_policies",
                  sa.Column("wfh_min_notice_days", sa.Integer(), nullable=False, server_default="1"))

    op.add_column("departments", sa.Column("manager_employee_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_departments_manager_employee_id", "departments", "employees",
        ["manager_employee_id"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_departments_manager_employee_id", "departments", ["manager_employee_id"])

    op.create_table(
        "wfh_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("half", sa.String(length=12), nullable=False, server_default="FULL"),
        sa.Column("days", sa.Numeric(6, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("applied_by_user_id", sa.Integer(), _admin_fk(), nullable=True),
        sa.Column("recorded_by_hr", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manager_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True),
        sa.Column("manager_action_by", sa.Integer(), _admin_fk(), nullable=True),
        sa.Column("manager_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manager_comment", sa.Text(), nullable=True),
        sa.Column("hr_action_by", sa.Integer(), _admin_fk(), nullable=True),
        sa.Column("hr_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hr_comment", sa.Text(), nullable=True),
        sa.Column("cancelled_by", sa.Integer(), _admin_fk(), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_wfh_requests_status", "wfh_requests", ["status"])
    op.create_index("ix_wfh_requests_employee_dates", "wfh_requests", ["employee_id", "from_date", "to_date"])


def downgrade() -> None:
    op.drop_index("ix_wfh_requests_employee_dates", table_name="wfh_requests")
    op.drop_index("ix_wfh_requests_status", table_name="wfh_requests")
    op.drop_table("wfh_requests")
    op.drop_index("ix_departments_manager_employee_id", table_name="departments")
    op.drop_constraint("fk_departments_manager_employee_id", "departments", type_="foreignkey")
    op.drop_column("departments", "manager_employee_id")
    op.drop_column("attendance_policies", "wfh_min_notice_days")
    op.drop_column("attendance_policies", "late_block_size")
