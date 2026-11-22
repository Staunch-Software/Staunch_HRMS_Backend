"""attendance devices, entry/exit device, HR-chosen Saturday off, permission LOP

Hand-written. Adds attendance_devices (seeded: Main Door IN_OUT, New Office
SINGLE, '' mobile app IN_OUT, plus any other device name already seen in
punches as IN_OUT), attendance_daily.entry_device / exit_device,
saturday_offs, and the policy columns saturday_off_mode,
permission_half_day_above_minutes, permission_full_day_above_minutes. Drops
the short-hours and late-deduction leave-order policy columns (worked hours
no longer cause LOP; late marks are straight LOP).

Existing daily rows keep their old entry/exit until recomputed: call
POST /api/v1/attendance/recompute for the affected range.

Revision ID: 71272a4984c2
Revises: 8742c8d3b70f
Create Date: 2026-11-15 23:30:00.000000

"""
import json
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "71272a4984c2"
down_revision: Union[str, None] = "8742c8d3b70f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

DEFAULT_DEVICES = [("Main Door", "Main Door", "IN_OUT"), ("New Office", "New Office", "SINGLE"), ("", "Mobile app", "IN_OUT")]


def _json():
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("attendance_daily", sa.Column("entry_device", sa.String(length=100), nullable=True))
    op.add_column("attendance_daily", sa.Column("exit_device", sa.String(length=100), nullable=True))

    devices = op.create_table(
        "attendance_devices",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("device_name", sa.String(length=100), nullable=False, server_default=""),
        sa.Column("display_name", sa.String(length=100), nullable=True),
        sa.Column("mode", sa.String(length=10), nullable=False, server_default="IN_OUT"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=150), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("device_name"),
    )
    bind = op.get_bind()
    now = datetime.now(timezone.utc)
    seen = {(name or "").strip() for (name,) in bind.execute(sa.text("SELECT DISTINCT device_name FROM attendance_punches"))}
    rows = [dict(device_name=n, display_name=d, mode=m, is_active=True, created_at=now, updated_at=now)
            for n, d, m in DEFAULT_DEVICES]
    known = {n for n, _, _ in DEFAULT_DEVICES}
    rows += [dict(device_name=n, display_name=n, mode="IN_OUT", is_active=True, created_at=now, updated_at=now)
             for n in sorted(seen - known)]
    op.bulk_insert(devices, rows)

    op.create_table(
        "saturday_offs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("month", sa.String(length=7), nullable=False),
        sa.Column("off_date", sa.Date(), nullable=False),
        sa.Column("work_location_id", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.String(length=150), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_location_id"], ["work_locations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("month", "work_location_id", name="uq_saturday_off_month_location"),
    )
    op.create_index(op.f("ix_saturday_offs_month"), "saturday_offs", ["month"])
    op.create_index(
        "uq_saturday_off_month_all", "saturday_offs", ["month"], unique=True,
        postgresql_where=sa.text("work_location_id IS NULL"), sqlite_where=sa.text("work_location_id IS NULL"),
    )

    op.add_column("attendance_policies", sa.Column(
        "saturday_off_mode", sa.String(length=20), nullable=False, server_default="HR_MONTHLY"))
    op.add_column("attendance_policies", sa.Column(
        "permission_half_day_above_minutes", sa.Integer(), nullable=False, server_default="120"))
    op.add_column("attendance_policies", sa.Column(
        "permission_full_day_above_minutes", sa.Integer(), nullable=False, server_default="240"))
    op.drop_column("attendance_policies", "late_deduction_leave_order")
    op.drop_column("attendance_policies", "short_hours_flag_max_minutes")
    op.drop_column("attendance_policies", "short_hours_half_day_max_minutes")


def downgrade() -> None:
    op.add_column("attendance_policies", sa.Column("short_hours_half_day_max_minutes", sa.Integer(), nullable=True))
    op.add_column("attendance_policies", sa.Column("short_hours_flag_max_minutes", sa.Integer(), nullable=True))
    op.add_column("attendance_policies", sa.Column("late_deduction_leave_order", _json(), nullable=True))
    policies = sa.table(
        "attendance_policies",
        sa.column("short_hours_flag_max_minutes", sa.Integer()),
        sa.column("short_hours_half_day_max_minutes", sa.Integer()),
        sa.column("late_deduction_leave_order", _json()),
    )
    op.get_bind().execute(policies.update().values(
        short_hours_flag_max_minutes=120, short_hours_half_day_max_minutes=240,
        late_deduction_leave_order=json.loads('["CL", "EL"]'),
    ))
    for column in ("short_hours_half_day_max_minutes", "short_hours_flag_max_minutes", "late_deduction_leave_order"):
        op.alter_column("attendance_policies", column, nullable=False)
    op.drop_column("attendance_policies", "permission_full_day_above_minutes")
    op.drop_column("attendance_policies", "permission_half_day_above_minutes")
    op.drop_column("attendance_policies", "saturday_off_mode")

    op.drop_index("uq_saturday_off_month_all", table_name="saturday_offs")
    op.drop_index(op.f("ix_saturday_offs_month"), table_name="saturday_offs")
    op.drop_table("saturday_offs")
    op.drop_table("attendance_devices")
    op.drop_column("attendance_daily", "exit_device")
    op.drop_column("attendance_daily", "entry_device")
