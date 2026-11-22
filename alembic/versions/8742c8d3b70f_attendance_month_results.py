"""attendance month results and the pay-group attendance_source param

Hand-written: the attendance_month_results table (monthly attendance
processing) and a data step that adds attendance_source to every existing
pay group's params (BIOMETRIC for CHENNAI_HO, INPUT for the others) when the
key is missing.

Revision ID: 8742c8d3b70f
Revises: 341f55da85cd
Create Date: 2026-11-15 22:00:00.000000

"""
import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "8742c8d3b70f"
down_revision: Union[str, None] = "341f55da85cd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _pay_groups():
    return sa.table(
        "pay_groups",
        sa.column("id", sa.Integer()),
        sa.column("code", sa.String()),
        sa.column("params", _json()),
    )


def _load(value):
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


def upgrade() -> None:
    op.create_table(
        "attendance_month_results",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("pay_group_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("lop_days", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("present_days", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("paid_leave_days", sa.Numeric(6, 2), nullable=False, server_default="0"),
        sa.Column("totals", _json(), nullable=False),
        sa.Column("days", _json(), nullable=False),
        sa.Column("warnings", _json(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processed_by", sa.String(length=150), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_by", sa.String(length=150), nullable=True),
        sa.Column("sent_snapshot", _json(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pay_group_id"], ["pay_groups.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", "month", name="uq_attendance_month_result"),
    )
    op.create_index(op.f("ix_attendance_month_results_employee_id"), "attendance_month_results", ["employee_id"])
    op.create_index(op.f("ix_attendance_month_results_month"), "attendance_month_results", ["month"])
    op.create_index(op.f("ix_attendance_month_results_pay_group_id"), "attendance_month_results", ["pay_group_id"])

    groups = _pay_groups()
    bind = op.get_bind()
    for group_id, code, params in bind.execute(sa.select(groups.c.id, groups.c.code, groups.c.params)).all():
        values = _load(params)
        if "attendance_source" in values:
            continue
        values["attendance_source"] = "BIOMETRIC" if code == "CHENNAI_HO" else "INPUT"
        bind.execute(groups.update().where(groups.c.id == group_id).values(params=values))


def downgrade() -> None:
    groups = _pay_groups()
    bind = op.get_bind()
    for group_id, params in bind.execute(sa.select(groups.c.id, groups.c.params)).all():
        values = _load(params)
        if values.pop("attendance_source", None) is not None:
            bind.execute(groups.update().where(groups.c.id == group_id).values(params=values))

    op.drop_index(op.f("ix_attendance_month_results_pay_group_id"), table_name="attendance_month_results")
    op.drop_index(op.f("ix_attendance_month_results_month"), table_name="attendance_month_results")
    op.drop_index(op.f("ix_attendance_month_results_employee_id"), table_name="attendance_month_results")
    op.drop_table("attendance_month_results")
