"""Missed-punch (attendance regularization) requests: an employee asks for a
missing IN and/or OUT time on a past day. Same approval workflow as
permission (the REGULARIZATION chain); final approval writes an
AttendanceOverride (source REGULARIZATION) that fills only the missing side."""
import datetime as dt
from datetime import time
from typing import Optional

from sqlalchemy import Date, ForeignKey, Index, Integer, String, Time
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.leave import _ApprovalFields

REASON_DEVICE = "DEVICE_NOT_WORKING"
REASON_POWER_CUT = "POWER_CUT"
REASON_OUTSIDE_DUTY = "OUTSIDE_DUTY"
REASON_FORGOT = "FORGOT"
REASON_OTHER = "OTHER"
REASON_LABELS = {
    REASON_DEVICE: "Device not working",
    REASON_POWER_CUT: "Power cut",
    REASON_OUTSIDE_DUTY: "Outside duty",
    REASON_FORGOT: "Forgot to punch",
    REASON_OTHER: "Other",
}
REASON_CODES = tuple(REASON_LABELS)


class AttendanceRegularization(Base, _ApprovalFields, TimestampMixin):
    __tablename__ = "attendance_regularizations"
    __table_args__ = (Index("ix_attendance_regularizations_employee_date", "employee_id", "date"),)

    approval_request_type = "REGULARIZATION"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    punch_in: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    punch_out: Mapped[Optional[time]] = mapped_column(Time, nullable=True)
    reason_code: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    # Filled on final approval: the sides actually written ("IN", "OUT",
    # "IN,OUT" or "" when the device data had arrived meanwhile).
    applied_sides: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    override_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("attendance_overrides.id", ondelete="SET NULL"), nullable=True
    )

    employee = relationship("Employee", foreign_keys="AttendanceRegularization.employee_id")
    manager = relationship("Employee", foreign_keys="AttendanceRegularization.manager_id")
