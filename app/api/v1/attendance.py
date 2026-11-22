import calendar
import re
from datetime import date, datetime, time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.attendance import AttendanceDaily, AttendanceOverride, AttendancePunch, AttendanceSyncRun
from app.models.employee import Employee
from app.models.user import AdminUser
from app.schemas.attendance import (
    AttendanceDailyRead,
    AttendanceMonthlySummary,
    AttendanceMonthlySummaryRow,
    AttendanceOverrideInfo,
    AttendanceOverrideRead,
    AttendanceOverrideUpsert,
    AttendancePunchRead,
    AttendanceRecomputeRequest,
    AttendanceRecomputeResult,
    AttendanceRelinkResult,
    AttendanceCatchUpResult,
    AttendanceSyncRequest,
    AttendanceSyncRunRead,
    AttendanceSyncStatus,
)
from app.services.attendance.coverage import catch_up, coverage_status
from app.services.attendance.devices import device_labels, label_of
from app.services.attendance.effective import active_overrides, worked_minutes
from app.services.attendance.overrides import (
    KIND_STATUS,
    KIND_TIMES,
    SOURCE_MANUAL,
    STATUS_CODES,
    STATUS_LEAVE,
    OverrideError,
    active_leave_codes,
    build_values,
    check_leave_code,
    parse_time_value,
    revert_override,
    save_override,
)
from app.services.attendance.recompute import RecomputeError, recompute_range
from app.services.attendance.sync import (
    MAX_SYNC_DAYS,
    SOURCE_FALLBACK_FIRST,
    SOURCE_FALLBACK_LAST,
    EmployeeRef,
    employee_code_map,
    relink_employees,
    sync_attendance,
)
from app.services.employee_code import normalize_employee_code

router = APIRouter(prefix="/attendance", tags=["attendance"], dependencies=[Depends(get_current_user)])


def _employee_fields(employee: Optional[EmployeeRef]) -> dict:
    if employee is None:
        return {"employee_id": None, "is_matched": False, "employee_name": None, "department": None}
    return {
        "employee_id": employee.id,
        "is_matched": True,
        "employee_name": employee.name,
        "department": employee.department,
    }


@router.post("/sync", response_model=AttendanceSyncRunRead)
def run_sync(payload: Optional[AttendanceSyncRequest] = None, db: Session = Depends(get_db)):
    """Synchronous manual sync. A Matrix failure is reported on the returned
    run (status="failed", error_message), not as an HTTP error."""
    payload = payload or AttendanceSyncRequest()
    date_from = payload.date_from or payload.date_to or date.today()
    date_to = payload.date_to or date_from
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="date_to must be on or after date_from.")
    if (date_to - date_from).days + 1 > MAX_SYNC_DAYS:
        raise HTTPException(status_code=400, detail=f"Sync range is limited to {MAX_SYNC_DAYS} days.")
    return sync_attendance(db, date_from, date_to, triggered_by="manual")


@router.get("/sync-status", response_model=AttendanceSyncStatus)
def sync_status(db: Session = Depends(get_db)):
    return coverage_status(db).to_json()


@router.post("/sync/catch-up", response_model=AttendanceCatchUpResult)
def run_catch_up(db: Session = Depends(get_db)):
    """Syncs from the last fully covered day to today, in chunks of
    MAX_SYNC_DAYS (at most 62 days back)."""
    runs = catch_up(db, triggered_by="catchup")
    return {"runs": runs, "status": coverage_status(db).to_json()}


@router.post("/recompute", response_model=AttendanceRecomputeResult)
def recompute(payload: AttendanceRecomputeRequest, db: Session = Depends(get_db)):
    """Rebuild the daily rows of the range (max 92 days) from the stored
    punches with the current device modes, without calling Matrix."""
    try:
        count = recompute_range(db, payload.date_from, payload.date_to)
    except RecomputeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return AttendanceRecomputeResult(date_from=payload.date_from, date_to=payload.date_to, days_recomputed=count)


@router.post("/relink", response_model=AttendanceRelinkResult)
def relink(db: Session = Depends(get_db)):
    """Re-match stored attendance to the employee master without calling Matrix."""
    changed = relink_employees(db)
    db.commit()
    return AttendanceRelinkResult(codes_relinked=changed)


SOURCE_OVERRIDE = "OVERRIDE"


def _daily_read(
    code: str,
    employee: Optional[EmployeeRef],
    work_date: date,
    daily: Optional[AttendanceDaily],
    override: Optional[AttendanceOverride],
    labels: Optional[dict] = None,
) -> AttendanceDailyRead:
    """Effective values: an active TIMES override replaces the side(s) it
    sets; the Matrix values stay in matrix_punch_in/out."""
    m_in = daily.punch_in if daily else None
    m_out = daily.punch_out if daily else None
    p_in, p_out = m_in, m_out
    in_source = daily.punch_in_source if daily else None
    out_source = daily.punch_out_source if daily else None
    in_device = daily.entry_device if daily else None
    out_device = daily.exit_device if daily else None
    if override is not None and override.kind == KIND_TIMES:
        if override.punch_in is not None:
            p_in, in_source, in_device = override.punch_in, SOURCE_OVERRIDE, None
        if override.punch_out is not None:
            p_out, out_source, out_device = override.punch_out, SOURCE_OVERRIDE, None
    minutes = daily.worked_minutes if daily and (p_in, p_out) == (m_in, m_out) else worked_minutes(p_in, p_out)
    return AttendanceDailyRead(
        id=daily.id if daily else None,
        employee_code=code,
        username=daily.username if daily else None,
        work_date=work_date,
        punch_in=p_in,
        punch_in_source=in_source,
        punch_out=p_out,
        punch_out_source=out_source,
        worked_minutes=minutes,
        punch_count=daily.punch_count if daily else 0,
        entry_device=label_of(labels or {}, in_device),
        exit_device=label_of(labels or {}, out_device),
        is_fallback_in=in_source == SOURCE_FALLBACK_FIRST,
        is_fallback_out=out_source == SOURCE_FALLBACK_LAST,
        is_single_punch=p_in is not None and p_out is None,
        matrix_punch_in=m_in,
        matrix_punch_out=m_out,
        override=AttendanceOverrideInfo.model_validate(override) if override is not None else None,
        is_override_only=daily is None,
        **_employee_fields(employee),
    )


@router.get("/daily", response_model=list[AttendanceDailyRead])
def list_daily(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    employee_code: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Matrix daily rows with active HR overrides applied, plus days that
    exist only as an override (no Matrix punches)."""
    today = date.today()
    date_from = date_from or date_to or today
    date_to = date_to or date_from
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="date_to must be on or after date_from.")
    employees = employee_code_map(db)
    stmt = select(AttendanceDaily).where(
        AttendanceDaily.work_date >= date_from, AttendanceDaily.work_date <= date_to
    )
    override_ids = None
    if employee_code:
        code = normalize_employee_code(employee_code)
        stmt = stmt.where(AttendanceDaily.employee_code == code)
        override_ids = [employees[code].id] if code in employees else []
    rows = db.scalars(stmt.order_by(AttendanceDaily.work_date.desc(), AttendanceDaily.employee_code)).all()
    overrides = active_overrides(db, date_from, date_to, override_ids)
    labels = device_labels(db)

    result = []
    for row in rows:
        employee = employees.get(row.employee_code)
        override = overrides.pop((employee.id, row.work_date), None) if employee else None
        result.append(_daily_read(row.employee_code, employee, row.work_date, row, override, labels))
    if overrides:
        code_by_id = {ref.id: code for code, ref in employees.items()}
        for (employee_id, work_date), override in overrides.items():
            code = code_by_id.get(employee_id, override.employee_code)
            result.append(_daily_read(code, employees.get(code), work_date, None, override))
        result.sort(key=lambda r: (-r.work_date.toordinal(), r.employee_code))
    return result


def _clock(value: Optional[str], work_date: date, label: str) -> Optional[time]:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        if "T" in text or (len(text) > 8 and "-" in text):
            moment = datetime.fromisoformat(text)
            if moment.date() != work_date:
                raise ValueError(f"must be on {work_date.isoformat()}")
            return moment.time().replace(microsecond=0)
        return parse_time_value(text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{label}: {exc}") from exc


def _require_employee(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return employee


@router.put("/daily/{employee_id}/{work_date}", response_model=AttendanceOverrideRead)
def save_day_override(
    employee_id: int,
    work_date: date,
    payload: AttendanceOverrideUpsert,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    """Manual correction for one employee-day. Replaces the active one (kept
    inactive for audit). A leave code sent as status_code is read as status
    LEAVE with that leave type."""
    employee = _require_employee(db, employee_id)
    reason = (payload.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="reason is required.")
    kind = (payload.kind or "").strip().upper()
    status_code = (payload.status_code or "").strip().upper() or None
    leave_code = (payload.leave_type_code or "").strip().upper() or None
    try:
        if kind == KIND_STATUS:
            if status_code and status_code not in STATUS_CODES and not leave_code:
                status_code, leave_code = STATUS_LEAVE, status_code
            if leave_code:
                leave_code, _ = check_leave_code(leave_code, active_leave_codes(db))
        values = build_values(
            kind=kind,
            work_date=work_date,
            punch_in=_clock(payload.punch_in, work_date, "punch_in") if kind == KIND_TIMES else None,
            punch_out=_clock(payload.punch_out, work_date, "punch_out") if kind == KIND_TIMES else None,
            status_code=status_code,
            leave_type_code=leave_code,
            half=payload.half,
            reason=reason,
        )
    except OverrideError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    row, _ = save_override(db, employee.id, employee.employee_code, work_date, values,
                           source=SOURCE_MANUAL, created_by=user.username)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="This day was changed at the same time - reload and retry.") from exc
    db.refresh(row)
    return row


@router.delete("/daily/{employee_id}/{work_date}", response_model=AttendanceOverrideRead)
def revert_day_override(
    employee_id: int,
    work_date: date,
    db: Session = Depends(get_db),
    user: AdminUser = Depends(get_current_user),
):
    """Deactivates the active correction; the day falls back to Matrix data."""
    _require_employee(db, employee_id)
    row = revert_override(db, employee_id, work_date, user.username)
    if row is None:
        raise HTTPException(status_code=404, detail="No active correction for this day.")
    db.commit()
    db.refresh(row)
    return row


@router.get("/overrides", response_model=list[AttendanceOverrideRead])
def list_overrides(
    employee_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    active_only: bool = False,
    limit: int = Query(500, ge=1, le=5000),
    db: Session = Depends(get_db),
):
    """Correction history, newest first (inactive rows included unless active_only)."""
    stmt = select(AttendanceOverride)
    if employee_id is not None:
        stmt = stmt.where(AttendanceOverride.employee_id == employee_id)
    if date_from is not None:
        stmt = stmt.where(AttendanceOverride.work_date >= date_from)
    if date_to is not None:
        stmt = stmt.where(AttendanceOverride.work_date <= date_to)
    if active_only:
        stmt = stmt.where(AttendanceOverride.is_active.is_(True))
    stmt = stmt.order_by(AttendanceOverride.work_date.desc(), AttendanceOverride.id.desc()).limit(limit)
    return db.scalars(stmt).all()


@router.get("/punches", response_model=list[AttendancePunchRead])
def list_punches(work_date: date, employee_code: Optional[str] = None, db: Session = Depends(get_db)):
    stmt = select(AttendancePunch).where(AttendancePunch.event_date == work_date)
    if employee_code:
        stmt = stmt.where(AttendancePunch.employee_code == normalize_employee_code(employee_code))
    rows = db.scalars(stmt.order_by(AttendancePunch.employee_code, AttendancePunch.event_datetime)).all()
    return [
        AttendancePunchRead.model_validate(
            {
                **{c: getattr(r, c) for c in (
                    "id", "employee_code", "raw_userid", "username", "event_datetime",
                    "event_date", "entry_exit_type", "device_name", "received_at",
                )},
                "direction": "IN" if r.entry_exit_type == "0" else "OUT",
            }
        )
        for r in rows
    ]


@router.get("/sync-runs", response_model=list[AttendanceSyncRunRead])
def list_sync_runs(limit: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)):
    return db.scalars(select(AttendanceSyncRun).order_by(AttendanceSyncRun.id.desc()).limit(limit)).all()


_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


@router.get("/monthly-summary", response_model=AttendanceMonthlySummary)
def monthly_summary(month: str = Query(..., description="YYYY-MM"), db: Session = Depends(get_db)):
    match = _MONTH_RE.match(month.strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format.")
    year, mon = int(match.group(1)), int(match.group(2))
    date_from = date(year, mon, 1)
    date_to = date(year, mon, calendar.monthrange(year, mon)[1])

    rows = db.scalars(
        select(AttendanceDaily)
        .where(AttendanceDaily.work_date >= date_from, AttendanceDaily.work_date <= date_to)
        .order_by(AttendanceDaily.employee_code, AttendanceDaily.work_date)
    ).all()
    totals: dict[str, dict] = {}
    for row in rows:
        entry = totals.setdefault(
            row.employee_code,
            {"username": None, "days_present": 0, "days_with_fallback_out": 0, "total_worked_minutes": 0},
        )
        if row.punch_count >= 1:
            entry["days_present"] += 1
        if row.punch_out_source == SOURCE_FALLBACK_LAST:
            entry["days_with_fallback_out"] += 1
        entry["total_worked_minutes"] += row.worked_minutes or 0
        entry["username"] = row.username or entry["username"]

    employees = employee_code_map(db)
    return AttendanceMonthlySummary(
        month=f"{year:04d}-{mon:02d}",
        date_from=date_from,
        date_to=date_to,
        employees=[
            AttendanceMonthlySummaryRow(employee_code=code, **values, **_employee_fields(employees.get(code)))
            for code, values in totals.items()
        ],
    )
