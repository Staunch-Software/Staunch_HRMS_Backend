"""Attendance data pulled from the Matrix COSEC (vyom) API.

AttendancePunch holds every raw punch exactly once; AttendanceDaily is
derived from it per (employee_code, work_date) and can always be rebuilt.
employee_code is the normalized code (app.services.employee_code); the
stored employee_id is a cache that the sync re-links, while read endpoints
resolve the employee live so newly imported employees match immediately.

AttendanceOverride is HR's correction layer on top of Matrix (uploads and
manual edits); see app.services.attendance.effective for how they combine.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, _utcnow


class AttendancePunch(Base):
    __tablename__ = "attendance_punches"
    __table_args__ = (
        UniqueConstraint(
            "employee_code", "event_datetime", "entry_exit_type", "device_name",
            name="uq_attendance_punch",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_code: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    raw_userid: Mapped[str] = mapped_column(String(50), nullable=False)
    username: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    event_datetime: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    entry_exit_type: Mapped[str] = mapped_column(String(1), nullable=False)
    device_name: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    received_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"), nullable=True, index=True)
    # MATRIX | MOBILE (HRMS app, models.mobile_attendance); NULL on rows older than the column.
    source: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    mobile_punch_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)


class AttendanceDaily(Base):
    __tablename__ = "attendance_daily"
    __table_args__ = (UniqueConstraint("employee_code", "work_date", name="uq_attendance_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_code: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    employee_id: Mapped[Optional[int]] = mapped_column(ForeignKey("employees.id"), nullable=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    punch_in: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    punch_in_source: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    punch_out: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    punch_out_source: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    worked_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    punch_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Device of the chosen entry / exit punch ('' = mobile app); None = no such punch.
    entry_device: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    exit_device: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


DEVICE_MODE_IN_OUT = "IN_OUT"
DEVICE_MODE_SINGLE = "SINGLE"
DEVICE_MODES = (DEVICE_MODE_IN_OUT, DEVICE_MODE_SINGLE)
# device_name -> (display_name, mode); '' is the Matrix mobile app,
# MOBILE_DEVICE_NAME the HRMS app's geofenced punches.
MOBILE_DEVICE_NAME = "HRMS Mobile"
DEFAULT_DEVICES = {
    "Main Door": ("Main Door", DEVICE_MODE_IN_OUT),
    "New Office": ("New Office", DEVICE_MODE_SINGLE),
    "": ("Mobile app", DEVICE_MODE_IN_OUT),
    MOBILE_DEVICE_NAME: ("HRMS app", DEVICE_MODE_IN_OUT),
}


class AttendanceDevice(Base):
    """How a Matrix device's punches become entry / exit: IN_OUT devices have
    separate IN and OUT readers (first IN, last OUT); SINGLE devices have one
    reader (first punch, last punch)."""

    __tablename__ = "attendance_devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, default="")
    display_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    mode: Mapped[str] = mapped_column(String(10), nullable=False, default=DEVICE_MODE_IN_OUT)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)


class AttendanceSyncRun(Base):
    __tablename__ = "attendance_sync_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    triggered_by: Mapped[str] = mapped_column(String(20), nullable=False)  # scheduler|manual
    date_from: Mapped[date] = mapped_column(Date, nullable=False)
    date_to: Mapped[date] = mapped_column(Date, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")  # running|success|failed
    records_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    records_new: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    days_recomputed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AttendanceOverride(Base):
    """One HR correction for an employee-day. kind TIMES replaces the
    punch-in/out (a blank side keeps the Matrix value); kind STATUS sets the
    day status (P, A, HD, WO, H, OD, LEAVE). At most one row per
    (employee_id, work_date) is active; a newer one deactivates the previous,
    which is kept for audit."""

    __tablename__ = "attendance_overrides"
    __table_args__ = (
        Index("ix_attendance_override_lookup", "employee_id", "work_date", "is_active"),
        Index(
            "uq_attendance_override_active", "employee_id", "work_date", unique=True,
            postgresql_where=text("is_active"), sqlite_where=text("is_active"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False)
    employee_code: Mapped[str] = mapped_column(String(30), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)  # TIMES|STATUS
    punch_in: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    punch_out: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    status_code: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    leave_type_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    half: Mapped[str] = mapped_column(String(12), nullable=False, default="FULL")  # FULL|FIRST_HALF|SECOND_HALF
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # UPLOAD_DAILY|UPLOAD_MUSTER|MANUAL
    import_batch_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    deactivated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
