"""Attendance data pulled from the Matrix COSEC (vyom) API.

AttendancePunch holds every raw punch exactly once; AttendanceDaily is
derived from it per (employee_code, work_date) and can always be rebuilt.
employee_code is the normalized code (app.services.employee_code); the
stored employee_id is a cache that the sync re-links, while read endpoints
resolve the employee live so newly imported employees match immediately.
"""
from datetime import date, datetime
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )


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
