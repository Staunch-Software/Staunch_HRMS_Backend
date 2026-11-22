"""Leave day counting.

Working days count 1 (0.5 for a half on the first/last day). Weekly offs and
holidays strictly between two counted leave days count as leave when the
leave type has the sandwich rule; offs before the first or after the last
working leave day never count.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import HALF_FIRST, HALF_FULL, HALF_SECOND, HALVES, LeaveType
from app.services.leave.calendar import KIND_WORK, day_kinds

ONE = Decimal("1")
HALF = Decimal("0.5")
ZERO = Decimal("0")


class LeaveDaysError(ValueError):
    pass


@dataclass
class LeaveDay:
    date: date
    kind: str  # WORK | WEEKLY_OFF | HOLIDAY
    holiday_name: Optional[str]
    counted: Decimal
    half: str  # FULL | FIRST_HALF | SECOND_HALF (the half taken on that date)
    sandwiched: bool = False


def validate_halves(from_date: date, from_half: str, to_date: date, to_half: str) -> tuple[str, str]:
    """Normalizes and validates the half-day selection. For a single day
    to_half follows from_half; across several days the leave may start in
    the second half of the first day and end in the first half of the last."""
    from_half = (from_half or HALF_FULL).upper()
    to_half = (to_half or HALF_FULL).upper()
    if from_half not in HALVES or to_half not in HALVES:
        raise LeaveDaysError("Half must be FULL, FIRST_HALF or SECOND_HALF.")
    if to_date < from_date:
        raise LeaveDaysError("The end date must be on or after the start date.")
    if from_date == to_date:
        return from_half, from_half
    if from_half == HALF_FIRST:
        raise LeaveDaysError("A leave spanning several days can only start with a full day or the second half.")
    if to_half == HALF_SECOND:
        raise LeaveDaysError("A leave spanning several days can only end with a full day or the first half.")
    return from_half, to_half


def leave_day_breakdown(
    db: Session,
    employee: Employee,
    leave_type: LeaveType,
    from_date: date,
    from_half: str,
    to_date: date,
    to_half: str,
) -> list[LeaveDay]:
    from_half, to_half = validate_halves(from_date, from_half, to_date, to_half)
    kinds = day_kinds(db, employee, from_date, to_date)
    days: list[LeaveDay] = []
    for day, (kind, holiday) in kinds.items():
        if day == from_date:
            half = from_half
        elif day == to_date:
            half = to_half
        else:
            half = HALF_FULL
        counted = (ONE if half == HALF_FULL else HALF) if kind == KIND_WORK else ZERO
        days.append(LeaveDay(day, kind, holiday.name if holiday else None, counted, half))

    working_indexes = [i for i, d in enumerate(days) if d.kind == KIND_WORK]
    if leave_type.sandwich_rule and len(working_indexes) >= 2:
        first, last = working_indexes[0], working_indexes[-1]
        for d in days[first + 1:last]:
            if d.kind != KIND_WORK:
                d.counted = ONE
                d.half = HALF_FULL
                d.sandwiched = True
    return days


def count_leave_days(
    db: Session,
    employee: Employee,
    leave_type: LeaveType,
    from_date: date,
    from_half: str,
    to_date: date,
    to_half: str,
) -> Decimal:
    days = leave_day_breakdown(db, employee, leave_type, from_date, from_half, to_date, to_half)
    return sum((d.counted for d in days), ZERO)


def half_slots(from_date: date, from_half: str, to_date: date, to_half: str) -> set[tuple[date, int]]:
    """(date, 1|2) half-day slots a request occupies, for overlap checks."""
    slots = set()
    day = from_date
    while day <= to_date:
        half = from_half if day == from_date else (to_half if day == to_date else HALF_FULL)
        if half in (HALF_FULL, HALF_FIRST):
            slots.add((day, 1))
        if half in (HALF_FULL, HALF_SECOND):
            slots.add((day, 2))
        day += timedelta(days=1)
    return slots
