"""attendance policy, holidays, leave types, leave/permission requests, leave ledger

Seeds the default attendance policy (company policy effective 01.11.2026)
and the CL / SL / EL / LOP leave types. Paid types start with an annual
entitlement of 0: HR must enter the real counts in Leave Types.

Revision ID: c90ae2316d3c
Revises: 27044f7c4111
Create Date: 2026-11-15 15:00:00.000000

"""
from datetime import date, datetime, time, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c90ae2316d3c"
down_revision: Union[str, None] = "27044f7c4111"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _approval_columns():
    return [
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("applied_by_user_id", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("recorded_by_hr", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manager_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="SET NULL"), nullable=True),
        sa.Column("manager_action_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("manager_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("manager_comment", sa.Text(), nullable=True),
        sa.Column("hr_action_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("hr_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hr_comment", sa.Text(), nullable=True),
        sa.Column("cancelled_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_comment", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    policies = op.create_table(
        "attendance_policies",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("work_location_id", sa.Integer(), sa.ForeignKey("work_locations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("shift_start", sa.Time(), nullable=False),
        sa.Column("grace_until", sa.Time(), nullable=False),
        sa.Column("required_minutes", sa.Integer(), nullable=False),
        sa.Column("weekday_pattern", _json(), nullable=False),
        sa.Column("saturdays_off", _json(), nullable=False),
        sa.Column("free_lates_per_month", sa.Integer(), nullable=False),
        sa.Column("late_deduction_days", sa.Numeric(4, 2), nullable=False),
        sa.Column("late_deduction_leave_order", _json(), nullable=False),
        sa.Column("permission_monthly_minutes", sa.Integer(), nullable=False),
        sa.Column("permission_max_per_month", sa.Integer(), nullable=False),
        sa.Column("permission_max_minutes_each", sa.Integer(), nullable=False),
        sa.Column("short_hours_flag_max_minutes", sa.Integer(), nullable=False),
        sa.Column("short_hours_half_day_max_minutes", sa.Integer(), nullable=False),
        sa.Column("missing_out_policy", sa.String(length=20), nullable=False),
        sa.Column("min_notice_days", sa.Integer(), nullable=False),
        sa.Column("leave_year_start_month", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.UniqueConstraint("work_location_id", name="uq_attendance_policies_work_location_id"),
    )

    op.create_table(
        "holidays",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("work_location_id", sa.Integer(), sa.ForeignKey("work_locations.id", ondelete="CASCADE"), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("date", "work_location_id", name="uq_holiday_date_location"),
    )
    op.create_index("ix_holidays_date", "holidays", ["date"])

    leave_types = op.create_table(
        "leave_types",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("annual_entitlement", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("credit_mode", sa.String(length=20), nullable=False, server_default="YEARLY_UPFRONT"),
        sa.Column("pro_rata_for_joiners", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("carry_forward", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("carry_forward_limit", sa.Numeric(6, 2), nullable=True),
        sa.Column("is_paid", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allow_half_day", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sandwich_rule", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("allowed_during_notice", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allow_negative_balance", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_leave_types_code", "leave_types", ["code"], unique=True)

    op.create_table(
        "leave_applications",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_types.id"), nullable=False),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("from_half", sa.String(length=12), nullable=False, server_default="FULL"),
        sa.Column("to_half", sa.String(length=12), nullable=False, server_default="FULL"),
        sa.Column("days", sa.Numeric(6, 2), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        *_approval_columns(),
    )
    op.create_index("ix_leave_applications_status", "leave_applications", ["status"])
    op.create_index("ix_leave_applications_employee_dates", "leave_applications", ["employee_id", "from_date", "to_date"])

    op.create_table(
        "permission_requests",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("from_time", sa.Time(), nullable=False),
        sa.Column("to_time", sa.Time(), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("override_limits", sa.Boolean(), nullable=False, server_default=sa.false()),
        *_approval_columns(),
    )
    op.create_index("ix_permission_requests_status", "permission_requests", ["status"])
    op.create_index("ix_permission_requests_employee_date", "permission_requests", ["employee_id", "date"])

    op.create_table(
        "leave_ledger",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("employee_id", sa.Integer(), sa.ForeignKey("employees.id", ondelete="CASCADE"), nullable=False),
        sa.Column("leave_type_id", sa.Integer(), sa.ForeignKey("leave_types.id"), nullable=False),
        sa.Column("leave_year", sa.Integer(), nullable=False),
        sa.Column("entry_type", sa.String(length=20), nullable=False),
        sa.Column("days", sa.Numeric(6, 2), nullable=False),
        sa.Column("reference_type", sa.String(length=30), nullable=True),
        sa.Column("reference_id", sa.String(length=40), nullable=True),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_leave_ledger_balance", "leave_ledger", ["employee_id", "leave_type_id", "leave_year"])
    op.create_index("ix_leave_ledger_reference", "leave_ledger", ["reference_type", "reference_id"])

    now = datetime.now(timezone.utc)
    op.bulk_insert(policies, [{
        "work_location_id": None,
        "shift_start": time(9, 30),
        "grace_until": time(9, 35),
        "required_minutes": 540,
        "weekday_pattern": {"mon": "WORK", "tue": "WORK", "wed": "WORK", "thu": "WORK", "fri": "WORK",
                            "sat": "WORK", "sun": "OFF"},
        "saturdays_off": [2],
        "free_lates_per_month": 2,
        "late_deduction_days": 0.5,
        "late_deduction_leave_order": ["CL", "EL"],
        "permission_monthly_minutes": 120,
        "permission_max_per_month": 2,
        "permission_max_minutes_each": 60,
        "short_hours_flag_max_minutes": 120,
        "short_hours_half_day_max_minutes": 240,
        "missing_out_policy": "PRESENT",
        "min_notice_days": 1,
        "leave_year_start_month": 1,
        "effective_from": date(2026, 11, 1),
        "updated_at": now,
        "updated_by": None,
    }])

    def leave_type(code, name, sort_order, **overrides):
        row = {
            "code": code, "name": name, "description": None, "annual_entitlement": 0,
            "credit_mode": "YEARLY_UPFRONT", "pro_rata_for_joiners": True, "carry_forward": False,
            "carry_forward_limit": None, "is_paid": True, "allow_half_day": True, "sandwich_rule": True,
            "allowed_during_notice": False, "allow_negative_balance": False, "is_active": True,
            "sort_order": sort_order, "created_at": now, "updated_at": now,
        }
        row.update(overrides)
        return row

    op.bulk_insert(leave_types, [
        leave_type("CL", "Casual Leave", 1),
        leave_type("SL", "Sick Leave", 2),
        leave_type("EL", "Earned Leave", 3, sandwich_rule=False),
        leave_type("LOP", "Loss of Pay", 4, is_paid=False, allow_negative_balance=True),
    ])


def downgrade() -> None:
    op.drop_index("ix_leave_ledger_reference", table_name="leave_ledger")
    op.drop_index("ix_leave_ledger_balance", table_name="leave_ledger")
    op.drop_table("leave_ledger")
    op.drop_index("ix_permission_requests_employee_date", table_name="permission_requests")
    op.drop_index("ix_permission_requests_status", table_name="permission_requests")
    op.drop_table("permission_requests")
    op.drop_index("ix_leave_applications_employee_dates", table_name="leave_applications")
    op.drop_index("ix_leave_applications_status", table_name="leave_applications")
    op.drop_table("leave_applications")
    op.drop_index("ix_leave_types_code", table_name="leave_types")
    op.drop_table("leave_types")
    op.drop_index("ix_holidays_date", table_name="holidays")
    op.drop_table("holidays")
    op.drop_table("attendance_policies")
