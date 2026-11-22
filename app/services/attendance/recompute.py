"""Rebuild AttendanceDaily rows from the stored punches for a date range,
e.g. after a device mode changed. Uses the same rules as the sync."""
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceDaily, AttendancePunch
from app.services.attendance.sync import employee_code_map, recompute_daily

MAX_RECOMPUTE_DAYS = 92
DEVICE_CHANGE_RECOMPUTE_DAYS = 62


class RecomputeError(ValueError):
    pass


def recompute_range(db: Session, date_from: date, date_to: date) -> int:
    """Recomputes every employee-day with punches (or a daily row) in the
    range. Returns the number of daily rows written. Does not commit."""
    if date_to < date_from:
        raise RecomputeError("date_to must be on or after date_from.")
    if (date_to - date_from).days + 1 > MAX_RECOMPUTE_DAYS:
        raise RecomputeError(f"Recompute range is limited to {MAX_RECOMPUTE_DAYS} days.")
    keys = {
        tuple(row) for row in db.execute(
            select(AttendancePunch.employee_code, AttendancePunch.event_date)
            .where(AttendancePunch.event_date >= date_from, AttendancePunch.event_date <= date_to)
            .distinct()
        )
    }
    keys |= {
        tuple(row) for row in db.execute(
            select(AttendanceDaily.employee_code, AttendanceDaily.work_date)
            .where(AttendanceDaily.work_date >= date_from, AttendanceDaily.work_date <= date_to)
        )
    }
    id_by_code = {code: ref.id for code, ref in employee_code_map(db).items()}
    return recompute_daily(db, keys, id_by_code)


def recompute_recent(db: Session, today: Optional[date] = None, days: int = DEVICE_CHANGE_RECOMPUTE_DAYS) -> tuple[date, date, int]:
    today = today or date.today()
    date_from = today - timedelta(days=days - 1)
    return date_from, today, recompute_range(db, date_from, today)
