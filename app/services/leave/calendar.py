"""Working-day calendar: which attendance policy applies to an employee and
whether a date is a working day, weekly off or holiday for them.

Saturdays under an HR_MONTHLY policy: only the month's SaturdayOff choice is
off (a location's own choice wins over the all-locations one); a month with
no choice assumes the 2nd Saturday and is reported by saturday_off_gaps.
"""
from datetime import date, timedelta
from typing import Mapping, Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.attendance_policy import (
    ASSUMED_SATURDAY_OFF,
    DAY_OFF,
    POLICY_DEFAULTS,
    SATURDAY_OFF_HR_MONTHLY,
    WEEKDAY_KEYS,
    AttendancePolicy,
    Holiday,
    SaturdayOff,
)
from app.models.employee import Employee

KIND_WORK = "WORK"
KIND_WEEKLY_OFF = "WEEKLY_OFF"
KIND_HOLIDAY = "HOLIDAY"


def default_policy(db: Session) -> AttendancePolicy:
    """The stored company default, or an unsaved one built from the policy
    defaults when none has been saved yet."""
    policy = db.query(AttendancePolicy).filter(AttendancePolicy.work_location_id.is_(None)).first()
    return policy or AttendancePolicy(work_location_id=None, **_fresh_defaults())


def ensure_default_policy(db: Session) -> AttendancePolicy:
    policy = db.query(AttendancePolicy).filter(AttendancePolicy.work_location_id.is_(None)).first()
    if policy is None:
        policy = AttendancePolicy(work_location_id=None, **_fresh_defaults())
        db.add(policy)
        db.flush()
    return policy


def _fresh_defaults() -> dict:
    values = dict(POLICY_DEFAULTS)
    values["weekday_pattern"] = dict(values["weekday_pattern"])
    values["saturdays_off"] = list(values["saturdays_off"])
    return values


def policy_for_location(db: Session, work_location_id: Optional[int]) -> AttendancePolicy:
    if work_location_id is not None:
        override = (
            db.query(AttendancePolicy).filter(AttendancePolicy.work_location_id == work_location_id).first()
        )
        if override is not None:
            return override
    return default_policy(db)


def policy_for(db: Session, employee: Employee) -> AttendancePolicy:
    """The employee's location override if one exists, else the default."""
    return policy_for_location(db, employee.work_location_id if employee else None)


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def saturdays_in_month(year: int, month: int) -> list[date]:
    day = date(year, month, 1)
    day += timedelta(days=(5 - day.weekday()) % 7)
    result = []
    while day.month == month:
        result.append(day)
        day += timedelta(days=7)
    return result


def assumed_saturday_off(year: int, month: int) -> date:
    return saturdays_in_month(year, month)[ASSUMED_SATURDAY_OFF - 1]


def monthly_saturday_mode(policy: AttendancePolicy) -> bool:
    return (getattr(policy, "saturday_off_mode", None) or SATURDAY_OFF_HR_MONTHLY) == SATURDAY_OFF_HR_MONTHLY


def _months(date_from: date, date_to: date) -> list[str]:
    months, day = [], date_from.replace(day=1)
    while day <= date_to:
        months.append(month_key(day))
        day = (day + timedelta(days=32)).replace(day=1)
    return months


def saturday_off_choices(
    db: Session, work_location_id: Optional[int], date_from: date, date_to: date
) -> dict[str, date]:
    """'YYYY-MM' -> HR's chosen Saturday off for that location (its own
    choice wins over the all-locations one), for the months in the range."""
    query = db.query(SaturdayOff).filter(SaturdayOff.month.in_(_months(date_from, date_to)))
    if work_location_id is None:
        query = query.filter(SaturdayOff.work_location_id.is_(None))
    else:
        query = query.filter(or_(SaturdayOff.work_location_id.is_(None), SaturdayOff.work_location_id == work_location_id))
    result = {}
    for row in sorted(query.all(), key=lambda r: r.work_location_id is not None):
        result[row.month] = row.off_date
    return result


def is_weekly_off(policy: AttendancePolicy, day: date, saturday_offs: Optional[Mapping[str, date]] = None) -> bool:
    """saturday_offs: saturday_off_choices for the employee's location; when
    omitted (or the month has no choice) HR_MONTHLY assumes the 2nd Saturday."""
    pattern = policy.weekday_pattern or {}
    if pattern.get(WEEKDAY_KEYS[day.weekday()]) == DAY_OFF:
        return True
    if day.weekday() != 5:
        return False
    if monthly_saturday_mode(policy):
        chosen = (saturday_offs or {}).get(month_key(day))
        return day == (chosen or assumed_saturday_off(day.year, day.month))
    nth_saturday = (day.day - 1) // 7 + 1
    return nth_saturday in {int(n) for n in (policy.saturdays_off or [])}


def saturday_off_gaps(
    db: Session, policy: AttendancePolicy, work_location_id: Optional[int], date_from: date, date_to: date
) -> list[tuple[str, date]]:
    """(month, assumed 2nd Saturday) for months in the range with no HR
    choice, when the policy needs one (HR_MONTHLY and Saturday a work day)."""
    if not monthly_saturday_mode(policy) or (policy.weekday_pattern or {}).get("sat") == DAY_OFF:
        return []
    choices = saturday_off_choices(db, work_location_id, date_from, date_to)
    return [(m, assumed_saturday_off(int(m[:4]), int(m[5:]))) for m in _months(date_from, date_to) if m not in choices]


def saturday_off_warning(month: str, assumed: date) -> str:
    return f"Saturday off for {month} not chosen — assumed 2nd Saturday ({assumed:%d %b})"


def _holiday_query(db: Session, work_location_id: Optional[int]):
    query = db.query(Holiday).filter(Holiday.is_active.is_(True))
    if work_location_id is None:
        return query.filter(Holiday.work_location_id.is_(None))
    return query.filter(or_(Holiday.work_location_id.is_(None), Holiday.work_location_id == work_location_id))


def holiday_on(db: Session, day: date, work_location_id: Optional[int]) -> Optional[Holiday]:
    """The active holiday on `day` for that location (company-wide holidays
    apply everywhere; a location-specific one wins when both exist)."""
    rows = _holiday_query(db, work_location_id).filter(Holiday.date == day).all()
    rows.sort(key=lambda h: h.work_location_id is None)
    return rows[0] if rows else None


def holidays_between(
    db: Session, date_from: date, date_to: date, work_location_id: Optional[int]
) -> dict[date, Holiday]:
    rows = (
        _holiday_query(db, work_location_id)
        .filter(Holiday.date >= date_from, Holiday.date <= date_to)
        .all()
    )
    result: dict[date, Holiday] = {}
    for holiday in sorted(rows, key=lambda h: h.work_location_id is not None):
        result[holiday.date] = holiday
    return result


def day_kind(db: Session, policy: AttendancePolicy, employee: Employee, day: date) -> str:
    """'HOLIDAY' takes precedence over 'WEEKLY_OFF' when both apply."""
    if holiday_on(db, day, employee.work_location_id if employee else None) is not None:
        return KIND_HOLIDAY
    location_id = employee.work_location_id if employee else None
    if is_weekly_off(policy, day, saturday_off_choices(db, location_id, day, day)):
        return KIND_WEEKLY_OFF
    return KIND_WORK


def day_kinds(
    db: Session, employee: Employee, date_from: date, date_to: date, policy: Optional[AttendancePolicy] = None
) -> dict[date, tuple[str, Optional[Holiday]]]:
    """Range version of day_kind: {date: (kind, holiday or None)}."""
    policy = policy or policy_for(db, employee)
    location_id = employee.work_location_id if employee else None
    holidays = holidays_between(db, date_from, date_to, location_id)
    saturday_offs = saturday_off_choices(db, location_id, date_from, date_to)
    result = {}
    day = date_from
    while day <= date_to:
        holiday = holidays.get(day)
        if holiday is not None:
            result[day] = (KIND_HOLIDAY, holiday)
        elif is_weekly_off(policy, day, saturday_offs):
            result[day] = (KIND_WEEKLY_OFF, None)
        else:
            result[day] = (KIND_WORK, None)
        day += timedelta(days=1)
    return result


def leave_year_of(policy: AttendancePolicy, day: date) -> int:
    """Leave years are named by the calendar year they start in."""
    start_month = policy.leave_year_start_month or 1
    return day.year if day.month >= start_month else day.year - 1


def leave_year_bounds(policy: AttendancePolicy, year: int) -> tuple[date, date]:
    start_month = policy.leave_year_start_month or 1
    start = date(year, start_month, 1)
    end = date(year + 1, start_month, 1) - timedelta(days=1) if start_month > 1 else date(year, 12, 31)
    return start, end
