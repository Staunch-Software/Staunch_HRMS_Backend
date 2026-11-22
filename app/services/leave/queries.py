"""Read helpers for attendance processing (see the package docstring)."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import STATUS_APPROVED, LeaveApplication, LeaveType, PermissionRequest
from app.services.leave.days import leave_day_breakdown


class LeaveDayEntry(NamedTuple):
    leave_type: LeaveType
    fraction: Decimal  # 1 or 0.5
    half: str  # FULL | FIRST_HALF | SECOND_HALF


def approved_leave_by_day(
    db: Session, employee_id: int, date_from: date, date_to: date
) -> dict[date, list[LeaveDayEntry]]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        return {}
    applications = (
        db.query(LeaveApplication)
        .filter(
            LeaveApplication.employee_id == employee_id,
            LeaveApplication.status == STATUS_APPROVED,
            LeaveApplication.from_date <= date_to,
            LeaveApplication.to_date >= date_from,
        )
        .order_by(LeaveApplication.from_date)
        .all()
    )
    result: dict[date, list[LeaveDayEntry]] = defaultdict(list)
    for application in applications:
        breakdown = leave_day_breakdown(
            db, employee, application.leave_type, application.from_date, application.from_half,
            application.to_date, application.to_half,
        )
        for day in breakdown:
            if date_from <= day.date <= date_to and day.counted > 0:
                result[day.date].append(LeaveDayEntry(application.leave_type, day.counted, day.half))
    return dict(result)


def approved_permission_minutes_by_day(
    db: Session, employee_id: int, date_from: date, date_to: date
) -> dict[date, int]:
    rows = (
        db.query(PermissionRequest.date, func.sum(PermissionRequest.minutes))
        .filter(
            PermissionRequest.employee_id == employee_id,
            PermissionRequest.status == STATUS_APPROVED,
            PermissionRequest.date >= date_from,
            PermissionRequest.date <= date_to,
        )
        .group_by(PermissionRequest.date)
        .all()
    )
    return {day: int(minutes or 0) for day, minutes in rows}


def approved_permissions_by_day(
    db: Session, employee_id: int, date_from: date, date_to: date
) -> dict[date, list[int]]:
    """Minutes of each APPROVED permission, per day (judged one by one)."""
    rows = (
        db.query(PermissionRequest.date, PermissionRequest.minutes)
        .filter(
            PermissionRequest.employee_id == employee_id,
            PermissionRequest.status == STATUS_APPROVED,
            PermissionRequest.date >= date_from,
            PermissionRequest.date <= date_to,
        )
        .order_by(PermissionRequest.date, PermissionRequest.id)
        .all()
    )
    result: dict[date, list[int]] = defaultdict(list)
    for day, minutes in rows:
        result[day].append(int(minutes or 0))
    return dict(result)
