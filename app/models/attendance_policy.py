"""Attendance policy settings and the holiday calendar.

One AttendancePolicy row with work_location_id NULL is the company default;
a row with a work_location_id overrides it for employees at that location.
Defaults follow the company attendance policy effective 01.11.2026.
"""
import datetime as dt
from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Time,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, _utcnow
from app.models.json_type import JSONType

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_WORK = "WORK"
DAY_OFF = "OFF"
MISSING_OUT_POLICIES = ("PRESENT", "HALF_DAY", "ABSENT")
# HR_MONTHLY: HR picks one Saturday off per month (SaturdayOff), assumed the
# 2nd Saturday until chosen. FIXED_NTH: the nth Saturdays in saturdays_off.
SATURDAY_OFF_HR_MONTHLY = "HR_MONTHLY"
SATURDAY_OFF_FIXED_NTH = "FIXED_NTH"
SATURDAY_OFF_MODES = (SATURDAY_OFF_HR_MONTHLY, SATURDAY_OFF_FIXED_NTH)
ASSUMED_SATURDAY_OFF = 2

POLICY_DEFAULTS = {
    "shift_start": time(9, 30),
    "grace_until": time(9, 35),
    "required_minutes": 540,
    "weekday_pattern": {k: (DAY_OFF if k == "sun" else DAY_WORK) for k in WEEKDAY_KEYS},
    "saturdays_off": [2],
    "saturday_off_mode": SATURDAY_OFF_HR_MONTHLY,
    "free_lates_per_month": 2,
    "late_deduction_days": Decimal("0.5"),
    "late_block_size": 3,
    "permission_monthly_minutes": 120,
    "permission_max_per_month": 2,
    "permission_max_minutes_each": 60,
    "permission_half_day_above_minutes": 120,
    "permission_full_day_above_minutes": 240,
    "missing_out_policy": "PRESENT",
    "late_upload_hold_days": 2,
    "regularization_max_back_days": 7,
    "regularization_monthly_cap": 3,
    "min_notice_days": 1,
    "wfh_min_notice_days": 1,
    "leave_year_start_month": 1,
    "effective_from": date(2026, 11, 1),
}


class AttendancePolicy(Base):
    __tablename__ = "attendance_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    work_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("work_locations.id", ondelete="CASCADE"), nullable=True, unique=True
    )

    shift_start: Mapped[time] = mapped_column(Time, nullable=False, default=POLICY_DEFAULTS["shift_start"])
    grace_until: Mapped[time] = mapped_column(Time, nullable=False, default=POLICY_DEFAULTS["grace_until"])
    required_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=540)
    weekday_pattern: Mapped[dict] = mapped_column(JSONType, nullable=False)
    saturdays_off: Mapped[list] = mapped_column(JSONType, nullable=False)
    saturday_off_mode: Mapped[str] = mapped_column(String(20), nullable=False, default=SATURDAY_OFF_HR_MONTHLY)

    free_lates_per_month: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    # Late marks beyond the free ones are straight LOP (not taken from leave):
    # every started block of late_block_size chargeable lates costs
    # late_deduction_days.
    late_deduction_days: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False, default=Decimal("0.5"))
    late_block_size: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")

    permission_monthly_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    permission_max_per_month: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    permission_max_minutes_each: Mapped[int] = mapped_column(Integer, nullable=False, default=60)

    # Each approved permission: <= half-day limit costs nothing; up to the
    # full-day limit 0.5 day LOP; above it 1 day LOP. required_minutes is
    # informational (worked hours never cause LOP).
    permission_half_day_above_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    permission_full_day_above_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=240)
    missing_out_policy: Mapped[str] = mapped_column(String(20), nullable=False, default="PRESENT")
    # Offline devices upload late: a day with IN but no OUT younger than this
    # many days is held (no missing-punch rule, no LOP) until the OUT arrives.
    late_upload_hold_days: Mapped[int] = mapped_column(Integer, nullable=False, default=2, server_default="2")
    # Missed-punch requests: how many days back, and how many per month.
    regularization_max_back_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7,
                                                              server_default="7")
    regularization_monthly_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")

    min_notice_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    wfh_min_notice_days: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    leave_year_start_month: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False, default=POLICY_DEFAULTS["effective_from"])
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    updated_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )

    work_location = relationship("WorkLocation")


class Holiday(Base, TimestampMixin):
    __tablename__ = "holidays"
    __table_args__ = (UniqueConstraint("date", "work_location_id", name="uq_holiday_date_location"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # NULL = applies to every location.
    work_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("work_locations.id", ondelete="CASCADE"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    work_location = relationship("WorkLocation")


class SaturdayOff(Base, TimestampMixin):
    """HR's chosen Saturday off for a month (HR_MONTHLY mode). work_location_id
    NULL applies to every location; a location's own row wins over it."""

    __tablename__ = "saturday_offs"
    __table_args__ = (
        UniqueConstraint("month", "work_location_id", name="uq_saturday_off_month_location"),
        Index(
            "uq_saturday_off_month_all", "month", unique=True,
            postgresql_where=text("work_location_id IS NULL"), sqlite_where=text("work_location_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # YYYY-MM
    off_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    work_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("work_locations.id", ondelete="CASCADE"), nullable=True
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    work_location = relationship("WorkLocation")
