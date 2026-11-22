"""Effective attendance per employee-day: Matrix daily rows with HR
overrides applied. This is the contract monthly processing reads.

effective_days(db, employee_ids, date_from, date_to)
    -> dict[(employee_id, work_date), EffectiveDay]

Only days with Matrix punches or an active override are present; a missing
key means "no data" (absent / week-off / holiday is decided by the caller).
employee_ids=None means every employee.

Matrix rows are tied to an employee by the live normalised employee_code
map first, then by the cached AttendanceDaily.employee_id. An active
override always wins over Matrix:

kind TIMES  (source OVERRIDE): punch_in / punch_out come from the override;
            a side the override leaves blank keeps the Matrix value.
            status_code / leave_type_code are None.
kind STATUS (source OVERRIDE): status_code decides the day; punch fields
            carry the Matrix values (informational only) and missing_out is
            False.
Matrix only (source MATRIX, kind TIMES): the Matrix first-IN / last-OUT.

worked_minutes is None unless both punches exist and out > in;
missing_out is True for a TIMES day with no punch_out.

status_code: P present, A absent, HD half day, WO week off, H holiday,
OD on duty (counts as present), LEAVE (leave_type_code says which).
HD with a leave_type_code = half present + half that leave; HD without one
= half present + half absent. half is FULL unless HR said which half the
status applies to (FIRST_HALF / SECOND_HALF): LEAVE + FIRST_HALF is a
first-half leave with the other half present.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceDaily, AttendanceOverride
from app.services.attendance.overrides import KIND_STATUS, KIND_TIMES
from app.services.attendance.sync import employee_code_map

SOURCE_MATRIX = "MATRIX"
SOURCE_OVERRIDE = "OVERRIDE"


@dataclass(frozen=True)
class EffectiveDay:
    employee_id: int
    work_date: date
    source: str  # MATRIX | OVERRIDE
    kind: str  # TIMES | STATUS
    punch_in: Optional[datetime]
    punch_out: Optional[datetime]
    worked_minutes: Optional[int]
    missing_out: bool
    status_code: Optional[str]
    leave_type_code: Optional[str]
    half: str
    punch_count: int
    override_id: Optional[int] = None
    override_source: Optional[str] = None  # UPLOAD_DAILY | UPLOAD_MUSTER | MANUAL
    reason: Optional[str] = None
    # Matrix device of the entry / exit punch ('' = mobile app); None when
    # there is no such punch or HR's correction replaced that side.
    entry_device: Optional[str] = None
    exit_device: Optional[str] = None


def worked_minutes(punch_in: Optional[datetime], punch_out: Optional[datetime]) -> Optional[int]:
    if punch_in is None or punch_out is None or punch_out <= punch_in:
        return None
    return int((punch_out - punch_in).total_seconds() // 60)


def active_overrides(
    db: Session, date_from: date, date_to: date, employee_ids: Optional[Iterable[int]] = None
) -> dict[tuple[int, date], AttendanceOverride]:
    stmt = select(AttendanceOverride).where(
        AttendanceOverride.is_active.is_(True),
        AttendanceOverride.work_date >= date_from,
        AttendanceOverride.work_date <= date_to,
    )
    if employee_ids is not None:
        ids = list(set(employee_ids))
        if not ids:
            return {}
        stmt = stmt.where(AttendanceOverride.employee_id.in_(ids))
    return {(o.employee_id, o.work_date): o for o in db.scalars(stmt)}


def matrix_days(
    db: Session, date_from: date, date_to: date, employee_ids: Optional[Iterable[int]] = None
) -> dict[tuple[int, date], AttendanceDaily]:
    """Matrix daily rows keyed by employee: live code map first, cached
    employee_id second. Rows that match no employee are left out."""
    code_map = employee_code_map(db)
    id_by_code = {code: ref.id for code, ref in code_map.items()}
    stmt = select(AttendanceDaily).where(
        AttendanceDaily.work_date >= date_from, AttendanceDaily.work_date <= date_to
    )
    wanted: Optional[set[int]] = None
    if employee_ids is not None:
        wanted = set(employee_ids)
        if not wanted:
            return {}
        codes = [code for code, emp_id in id_by_code.items() if emp_id in wanted]
        stmt = stmt.where(or_(AttendanceDaily.employee_code.in_(codes), AttendanceDaily.employee_id.in_(wanted)))

    result: dict[tuple[int, date], AttendanceDaily] = {}
    live_keys: set[tuple[int, date]] = set()
    for row in db.scalars(stmt.order_by(AttendanceDaily.id)):
        live_id = id_by_code.get(row.employee_code)
        emp_id = live_id if live_id is not None else row.employee_id
        if emp_id is None or (wanted is not None and emp_id not in wanted):
            continue
        key = (emp_id, row.work_date)
        if key in live_keys:
            continue
        result[key] = row
        if live_id is not None:
            live_keys.add(key)
    return result


def merge_override(
    employee_id: int, work_date: date, daily: Optional[AttendanceDaily], override: Optional[AttendanceOverride]
) -> EffectiveDay:
    m_in = daily.punch_in if daily else None
    m_out = daily.punch_out if daily else None
    count = daily.punch_count if daily else 0
    in_device = daily.entry_device if daily else None
    out_device = daily.exit_device if daily else None
    if override is None:
        return EffectiveDay(
            employee_id=employee_id, work_date=work_date, source=SOURCE_MATRIX, kind=KIND_TIMES,
            punch_in=m_in, punch_out=m_out, worked_minutes=worked_minutes(m_in, m_out),
            missing_out=m_out is None, status_code=None, leave_type_code=None, half="FULL", punch_count=count,
            entry_device=in_device, exit_device=out_device,
        )
    common = dict(
        employee_id=employee_id, work_date=work_date, source=SOURCE_OVERRIDE, kind=override.kind,
        half=override.half or "FULL", punch_count=count, override_id=override.id,
        override_source=override.source, reason=override.reason,
    )
    if override.kind == KIND_STATUS:
        return EffectiveDay(
            punch_in=m_in, punch_out=m_out, worked_minutes=worked_minutes(m_in, m_out), missing_out=False,
            status_code=override.status_code, leave_type_code=override.leave_type_code,
            entry_device=in_device, exit_device=out_device, **common,
        )
    p_in = override.punch_in if override.punch_in is not None else m_in
    p_out = override.punch_out if override.punch_out is not None else m_out
    return EffectiveDay(
        punch_in=p_in, punch_out=p_out, worked_minutes=worked_minutes(p_in, p_out), missing_out=p_out is None,
        status_code=None, leave_type_code=None, **common,
        entry_device=in_device if override.punch_in is None else None,
        exit_device=out_device if override.punch_out is None else None,
    )


def effective_days(
    db: Session, employee_ids: Optional[Iterable[int]], date_from: date, date_to: date
) -> dict[tuple[int, date], EffectiveDay]:
    ids = None if employee_ids is None else set(employee_ids)
    dailies = matrix_days(db, date_from, date_to, ids)
    overrides = active_overrides(db, date_from, date_to, ids)
    return {
        key: merge_override(key[0], key[1], dailies.get(key), overrides.get(key))
        for key in sorted(set(dailies) | set(overrides))
    }
