"""Missed-punch requests (attendance regularizations)

Adds the attendance_regularizations table and the attendance policy settings
regularization_max_back_days and regularization_monthly_cap. Hand-written.

Revision ID: 65654a5bc391
Revises: ab08ed3cb50d
Create Date: 2026-11-20 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '65654a5bc391'
down_revision: Union[str, None] = 'ab08ed3cb50d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _admin_fk():
    return sa.ForeignKey("admin_users.id", ondelete="SET NULL")


def upgrade() -> None:
    op.create_table(
        "attendance_regularizations",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("punch_in", sa.Time(), nullable=True),
        sa.Column("punch_out", sa.Time(), nullable=True),
        sa.Column("reason_code", sa.String(length=30), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("applied_sides", sa.String(length=10), nullable=True),
        sa.Column("override_id", sa.Integer(),
                  sa.ForeignKey("attendance_overrides.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("applied_by_user_id", sa.Integer(), _admin_fk(), nullable=True),
        sa.Column("recorded_by_hr", sa.Boolean(), nullable=False),
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
    op.create_index("ix_attendance_regularizations_status", "attendance_regularizations", ["status"])
    op.create_index("ix_attendance_regularizations_employee_date", "attendance_regularizations",
                    ["employee_id", "date"])
    op.add_column("attendance_policies", sa.Column("regularization_max_back_days", sa.Integer(), nullable=False,
                                                   server_default="7"))
    op.add_column("attendance_policies", sa.Column("regularization_monthly_cap", sa.Integer(), nullable=False,
                                                   server_default="3"))


def downgrade() -> None:
    op.drop_column("attendance_policies", "regularization_monthly_cap")
    op.drop_column("attendance_policies", "regularization_max_back_days")
    op.drop_index("ix_attendance_regularizations_employee_date", table_name="attendance_regularizations")
    op.drop_index("ix_attendance_regularizations_status", table_name="attendance_regularizations")
    op.drop_table("attendance_regularizations")
