"""Monthly attendance processing (HR): process a month for a pay group,
review the results, open one employee's days and send the LOP to payroll.
See app.services.attendance.monthly."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.attendance_month import AttendanceMonthResult
from app.models.employee import Employee
from app.models.pay_group import PayGroup
from app.models.user import AdminUser
from app.schemas.attendance_monthly import (
    MonthlyEmployeeDetail,
    MonthlyProcessRequest,
    MonthlyProcessResult,
    MonthlyResultList,
    MonthlyResultRow,
    MonthlySendRequest,
    MonthlySendResult,
)
from app.services.attendance import monthly
from app.services.attendance.coverage import uncovered_dates
from app.services.leave.calendar import policy_for_location
from app.services.payroll_engine.pay_groups import attendance_source_of
from app.services.reporting import employee_full_name

router = APIRouter(prefix="/attendance", tags=["attendance-monthly"], dependencies=[Depends(get_current_user)])


def _raise(exc: monthly.MonthlyError):
    raise HTTPException(status_code=exc.status_code, detail=exc.detail or exc.message) from exc


def _row_read(row: AttendanceMonthResult, employee: Optional[Employee]) -> MonthlyResultRow:
    return MonthlyResultRow(
        employee_id=row.employee_id,
        employee_code=employee.employee_code if employee else None,
        employee_name=employee_full_name(employee),
        department=employee.department.name if employee and employee.department else None,
        status=row.status,
        changed_since_sent=monthly.changed_since_sent(row),
        lop_days=row.lop_days,
        present_days=row.present_days,
        paid_leave_days=row.paid_leave_days,
        totals=row.totals or {},
        warnings=row.warnings or [],
        processed_at=row.processed_at,
        processed_by=row.processed_by,
        sent_at=row.sent_at,
        sent_by=row.sent_by,
    )


def _readiness(db: Session, scope: list[Employee], first: date, last: date) -> dict:
    """Past days of the month the sync has not fully covered, and the first
    day still inside the late-upload hold (longest hold of the scope's
    locations)."""
    today = date.today()
    policies = [policy_for_location(db, loc) for loc in {e.work_location_id for e in scope} or {None}]
    policy = max(policies, key=monthly.late_upload_hold_days)
    unsettled = monthly.unsettled_from(last, policy, today) if first <= today else None
    return {
        "uncovered_sync_dates": uncovered_dates(db, first, last, today),
        "unsettled_from": max(unsettled, first) if unsettled else None,
        "late_upload_hold_days": monthly.late_upload_hold_days(policy),
    }


def _readiness_warnings(readiness: dict) -> list[str]:
    warnings = []
    gaps = readiness["uncovered_sync_dates"]
    if gaps:
        listed = ", ".join(f"{d:%d %b}" for d in gaps[:10]) + (" ..." if len(gaps) > 10 else "")
        warnings.append(f"Attendance sync has not fully covered {len(gaps)} past day(s) ({listed}): "
                        "sync them before processing.")
    if readiness["unsettled_from"]:
        warnings.append(f"Days from {readiness['unsettled_from']:%d %b} are inside the "
                        f"{readiness['late_upload_hold_days']}-day late-upload hold: OUT punches from offline "
                        "devices may still arrive.")
    return warnings


def _result_list(db: Session, group: PayGroup, month: str) -> dict:
    ref, first, last = monthly.parse_month(month)
    rows = (
        db.query(AttendanceMonthResult)
        .filter(AttendanceMonthResult.month == ref, AttendanceMonthResult.pay_group_id == group.id)
        .all()
    )
    ids = [r.employee_id for r in rows]
    employees = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(ids))} if ids else {}
    results = sorted(
        (_row_read(r, employees.get(r.employee_id)) for r in rows), key=lambda r: r.employee_code or ""
    )
    scope = monthly.group_scope(db, group, first, last)
    processed_ids = set(ids)
    not_processed = [e.employee_code for e in scope if e.id not in processed_ids]
    no_data = [r.employee_code or str(r.employee_id) for r in results if not r.totals.get("days_with_data")]
    missing = sum(int(r.totals.get("missing_punch_days") or 0) for r in results)
    unmatched = monthly.unmatched_punch_codes(db, first, last)
    changed = sum(1 for r in results if r.changed_since_sent)
    upcoming = max((int(r.totals.get("upcoming_days") or 0) for r in results), default=0)
    saturday = monthly.saturday_off_warnings(db, scope, ref)
    if not saturday and any(w.startswith("Saturday off for") for r in results for w in r.warnings):
        saturday.append(f"The Saturday off for {ref} was chosen after some results were processed: "
                        "process the month again.")

    readiness = _readiness(db, scope, first, last)
    warnings = list(saturday) + _readiness_warnings(readiness)
    no_punch = sorted({date.fromisoformat(x) for r in results for x in (r.totals.get("no_punch_dates") or [])})
    if no_punch:
        warnings.append(monthly.no_punch_warning(no_punch))
    if not_processed:
        warnings.append(f"{len(not_processed)} employee(s) in the group are not processed yet: "
                        f"{', '.join(not_processed[:20])}{' ...' if len(not_processed) > 20 else ''}.")
    if no_data:
        warnings.append(f"{len(no_data)} employee(s) have no attendance data for {ref} (mostly LOP): "
                        f"{', '.join(no_data[:20])}{' ...' if len(no_data) > 20 else ''}.")
    if missing:
        warnings.append(f"{missing} day(s) with a missing punch across the group.")
    if unmatched:
        warnings.append(f"{len(unmatched)} biometric code(s) with punches in {ref} match no employee: "
                        f"{', '.join(unmatched[:20])}{' ...' if len(unmatched) > 20 else ''}.")
    if changed:
        warnings.append(f"{changed} result(s) changed since they were sent to payroll; send again to update payroll.")
    if upcoming:
        warnings.append(f"{ref} is still in progress: {upcoming} day(s) are not evaluated yet.")

    return dict(
        month=ref, date_from=first, date_to=last, pay_group_id=group.id, pay_group_code=group.code,
        pay_group_name=group.name, calc_model=group.calc_model, attendance_source=attendance_source_of(group),
        results=results, warnings=warnings, saturday_off_warnings=saturday, employees_in_scope=len(scope),
        not_processed=not_processed,
        no_data_employees=no_data, missing_punch_days=missing, unmatched_punch_codes=unmatched,
        total_lop=sum((r.lop_days for r in results), monthly.ZERO),
        **readiness,
    )


@router.post("/monthly/process", response_model=MonthlyProcessResult)
def process_month(
    payload: MonthlyProcessRequest, db: Session = Depends(get_db), user: AdminUser = Depends(get_current_user)
):
    try:
        summary = monthly.process_month(db, payload.month, payload.pay_group_id, user,
                                        employee_ids=payload.employee_ids)
        group = monthly.processable_group(db, payload.pay_group_id)
    except monthly.MonthlyError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    return MonthlyProcessResult(
        **_result_list(db, group, summary["month"]),
        processed=summary["processed"], created=summary["created"], removed=summary["removed"],
    )


@router.get("/monthly", response_model=MonthlyResultList)
def list_month_results(
    month: str = Query(..., description="YYYY-MM"), pay_group_id: int = Query(...), db: Session = Depends(get_db)
):
    try:
        monthly.parse_month(month)
        group = monthly.processable_group(db, pay_group_id)
    except monthly.MonthlyError as exc:
        _raise(exc)
    return MonthlyResultList(**_result_list(db, group, month))


@router.get("/monthly/{employee_id}", response_model=MonthlyEmployeeDetail)
def month_detail(employee_id: int, month: str = Query(..., description="YYYY-MM"), db: Session = Depends(get_db)):
    """The stored result when the month was processed for the employee,
    else the days resolved now (status NOT_PROCESSED)."""
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    try:
        ref, first, last = monthly.parse_month(month)
    except monthly.MonthlyError as exc:
        _raise(exc)
    base = dict(employee_id=employee.id, employee_code=employee.employee_code,
                employee_name=employee_full_name(employee), month=ref, date_from=first, date_to=last)
    row = (
        db.query(AttendanceMonthResult)
        .filter(AttendanceMonthResult.employee_id == employee.id, AttendanceMonthResult.month == ref)
        .first()
    )
    if row is None:
        result = monthly.resolve_month(db, employee, ref)
        return MonthlyEmployeeDetail(**base, status="NOT_PROCESSED", totals=result.totals,
                                     days=result.days_json(), warnings=result.warnings)
    return MonthlyEmployeeDetail(
        **base, status=row.status, changed_since_sent=monthly.changed_since_sent(row), pay_group_id=row.pay_group_id,
        totals=row.totals or {}, days=row.days or [], warnings=row.warnings or [], processed_at=row.processed_at,
        processed_by=row.processed_by, sent_at=row.sent_at, sent_by=row.sent_by,
    )


@router.post("/monthly/send-to-payroll", response_model=MonthlySendResult)
def send_to_payroll(
    payload: MonthlySendRequest, db: Session = Depends(get_db), user: AdminUser = Depends(get_current_user)
):
    try:
        summary = monthly.send_to_payroll(db, payload.month, payload.pay_group_id, user, payload.employee_ids)
    except monthly.MonthlyError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    return MonthlySendResult(**summary)
