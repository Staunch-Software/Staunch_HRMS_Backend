import calendar
import re
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.attendance import AttendanceDaily, AttendancePunch, AttendanceSyncRun
from app.schemas.attendance import (
    AttendanceDailyRead,
    AttendanceMonthlySummary,
    AttendanceMonthlySummaryRow,
    AttendancePunchRead,
    AttendanceRelinkResult,
    AttendanceSyncRequest,
    AttendanceSyncRunRead,
)
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


@router.post("/relink", response_model=AttendanceRelinkResult)
def relink(db: Session = Depends(get_db)):
    """Re-match stored attendance to the employee master without calling Matrix."""
    changed = relink_employees(db)
    db.commit()
    return AttendanceRelinkResult(codes_relinked=changed)


@router.get("/daily", response_model=list[AttendanceDailyRead])
def list_daily(
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    employee_code: Optional[str] = None,
    db: Session = Depends(get_db),
):
    today = date.today()
    date_from = date_from or date_to or today
    date_to = date_to or date_from
    if date_to < date_from:
        raise HTTPException(status_code=400, detail="date_to must be on or after date_from.")
    stmt = select(AttendanceDaily).where(
        AttendanceDaily.work_date >= date_from, AttendanceDaily.work_date <= date_to
    )
    if employee_code:
        stmt = stmt.where(AttendanceDaily.employee_code == normalize_employee_code(employee_code))
    rows = db.scalars(stmt.order_by(AttendanceDaily.work_date.desc(), AttendanceDaily.employee_code)).all()

    employees = employee_code_map(db)
    result = []
    for row in rows:
        result.append(
            AttendanceDailyRead(
                id=row.id,
                employee_code=row.employee_code,
                username=row.username,
                work_date=row.work_date,
                punch_in=row.punch_in,
                punch_in_source=row.punch_in_source,
                punch_out=row.punch_out,
                punch_out_source=row.punch_out_source,
                worked_minutes=row.worked_minutes,
                punch_count=row.punch_count,
                is_fallback_in=row.punch_in_source == SOURCE_FALLBACK_FIRST,
                is_fallback_out=row.punch_out_source == SOURCE_FALLBACK_LAST,
                is_single_punch=row.punch_out is None,
                **_employee_fields(employees.get(row.employee_code)),
            )
        )
    return result


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
