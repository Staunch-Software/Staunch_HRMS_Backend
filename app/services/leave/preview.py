"""Day-count preview for the leave forms (shows the sandwich effect)."""
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import HALF_FULL, LeaveType
from app.schemas.leave import LeaveDayRead, LeaveDaysPreview
from app.services.leave import balance as balance_service
from app.services.leave.days import LeaveDaysError, leave_day_breakdown, validate_halves

ZERO = Decimal("0")


def leave_days_preview(
    db: Session, employee: Employee, leave_type_id: int, from_date: date, from_half: str, to_date: date,
    to_half: str,
) -> LeaveDaysPreview:
    leave_type = db.get(LeaveType, leave_type_id)
    if leave_type is None:
        raise HTTPException(status_code=404, detail="Leave type not found.")
    year = balance_service.leave_year_for(db, from_date)
    errors = []
    breakdown = []
    try:
        from_half, to_half = validate_halves(from_date, from_half, to_date, to_half)
        if not leave_type.allow_half_day and (from_half != HALF_FULL or to_half != HALF_FULL):
            errors.append(f"{leave_type.name} cannot be taken as a half day.")
        if (to_date - from_date).days > 366:
            errors.append("The date range is too long.")
        else:
            breakdown = leave_day_breakdown(db, employee, leave_type, from_date, from_half, to_date, to_half)
    except LeaveDaysError as exc:
        errors.append(str(exc))
    if not errors and balance_service.leave_year_for(db, to_date) != year:
        errors.append("A leave cannot span two leave years; apply separately for each year.")
    days = sum((d.counted for d in breakdown), ZERO)

    bal = available = None
    if leave_type.tracks_balance:
        bal = balance_service.balance(db, employee.id, leave_type.id, year)
        available = bal - balance_service.pending_days(db, employee.id, leave_type.id, year)
    return LeaveDaysPreview(
        days=days,
        breakdown=[
            LeaveDayRead(date=d.date, kind=d.kind, holiday_name=d.holiday_name, counted=d.counted, half=d.half,
                         sandwiched=d.sandwiched)
            for d in breakdown
        ],
        leave_year=year,
        balance=bal,
        available=available,
        tracks_balance=leave_type.tracks_balance,
        errors=errors,
    )
