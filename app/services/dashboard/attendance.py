"""Dashboard attendance figures, read from effective attendance (Matrix daily
rows with HR overrides applied).

present  = active employees with an IN punch, or an override marking them
           P / OD / HD (or a half-day leave) for the day, or with approved
           work from home (full or half day) on a working day.
expected = active employees, joined by then, for whom the day is a working
           day at their location, minus those on full-day approved leave.
percent  = present-and-expected / expected.
Days use today's active employees, so past days ignore since-exited staff.
"""
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceDaily, AttendanceOverride
from app.services.attendance.effective import effective_days
from app.services.attendance.overrides import KIND_STATUS
from app.services.dashboard.people import active_employees, full_day_leave_ids
from app.services.leave.calendar import (
    default_policy,
    holidays_between,
    is_weekly_off,
    policy_for_location,
    saturday_off_choices,
)
from app.services.leave.wfh import approved_wfh_ids

TREND_DAYS = 30
PRESENT_CODES = {"P", "OD", "HD"}


def latest_data_day(db: Session, on_or_before: date) -> Optional[date]:
    daily = db.scalar(select(func.max(AttendanceDaily.work_date)).where(AttendanceDaily.work_date <= on_or_before))
    override = db.scalar(
        select(func.max(AttendanceOverride.work_date)).where(
            AttendanceOverride.is_active.is_(True), AttendanceOverride.work_date <= on_or_before
        )
    )
    return max((d for d in (daily, override) if d is not None), default=None)


def _is_present(day) -> bool:
    if day.kind == KIND_STATUS:
        return day.status_code in PRESENT_CODES or (day.status_code == "LEAVE" and day.half != "FULL")
    return day.punch_in is not None


def _working_ids(db: Session, employees: list, start: date, end: date) -> dict[date, set[int]]:
    by_location = defaultdict(list)
    for employee in employees:
        by_location[employee.work_location_id].append(employee)
    result: dict[date, set[int]] = defaultdict(set)
    for location_id, staff in by_location.items():
        policy = policy_for_location(db, location_id)
        holidays = holidays_between(db, start, end, location_id)
        saturday_offs = saturday_off_choices(db, location_id, start, end)
        day = start
        while day <= end:
            if day not in holidays and not is_weekly_off(policy, day, saturday_offs):
                result[day].update(e.id for e in staff if e.date_of_joining is None or e.date_of_joining <= day)
            day += timedelta(days=1)
    return result


def _percent(part: int, whole: int) -> Optional[float]:
    return round(part * 100.0 / whole, 1) if whole else None


def attendance_overview(db: Session, as_of: date) -> tuple[dict, list[dict]]:
    """(today's figures, per-day trend). When `as_of` has no attendance data
    yet, the figures are for the latest earlier day that has some."""
    data_day = latest_data_day(db, as_of)
    day = data_day or as_of
    start = day - timedelta(days=TREND_DAYS - 1)

    employees = active_employees(db)
    active_ids = {e.id for e in employees}
    working = _working_ids(db, employees, start, day)
    on_leave = full_day_leave_ids(db, start, day)
    effective = {
        key: eff for key, eff in effective_days(db, None, start, day).items() if key[0] in active_ids
    }

    present_by_day: dict[date, set[int]] = defaultdict(set)
    for (employee_id, work_date), eff in effective.items():
        if _is_present(eff):
            present_by_day[work_date].add(employee_id)
    for work_date, ids in approved_wfh_ids(db, start, day).items():
        present_by_day[work_date].update(ids & working.get(work_date, set()))

    trend = []
    cursor = start
    while cursor <= day:
        expected = working.get(cursor, set()) - on_leave.get(cursor, set())
        present = present_by_day.get(cursor, set())
        trend.append({
            "date": cursor, "present": len(present), "expected": len(expected),
            "percent": _percent(len(present & expected), len(expected)),
        })
        cursor += timedelta(days=1)

    grace = default_policy(db).grace_until
    late = sum(
        1 for (employee_id, work_date), eff in effective.items()
        if work_date == day and eff.kind != KIND_STATUS and eff.punch_in is not None
        and eff.punch_in.time() > grace
    )
    latest = trend[-1]
    today = {
        "date": day, "is_today": day == as_of, "has_data": data_day is not None,
        "present": latest["present"], "expected": latest["expected"], "percent": latest["percent"],
        "late": late, "late_after": grace.strftime("%H:%M"),
    }
    return today, trend
