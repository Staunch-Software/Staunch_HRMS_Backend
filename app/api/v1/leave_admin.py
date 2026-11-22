"""HR leave administration: leave types, the leave and permission request
queues, recording on behalf of employees, and balances."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_hr
from app.models.employee import Employee
from app.models.leave import (
    PENDING_STATUSES,
    STATUSES,
    LeaveApplication,
    LeaveLedger,
    LeaveType,
    PermissionRequest,
)
from app.models.user import AdminUser
from app.models.wfh import WfhRequest
from app.models.regularization import AttendanceRegularization
from app.schemas.leave import (
    ActionComment,
    BalanceAdjustRequest,
    BalanceRow,
    CreditCatchUpRequest,
    CreditMonthRequest,
    CreditRunRequest,
    CreditRunResult,
    EmployeeBalances,
    MonthlyCreditResult,
    LeaveApplicationRead,
    LeaveDaysPreview,
    LeaveRecordRequest,
    LeaveTypeCreate,
    LeaveTypeRead,
    LeaveTypeUpdate,
    LedgerEntryRead,
    PendingCounts,
    PermissionAllowance,
    PermissionRecordRequest,
    PermissionRequestRead,
)
from app.services.leave import balance as balance_service
from app.services.leave import workflow
from app.services.leave.preview import leave_days_preview
from app.services.leave.serialize import leave_read, leave_reads, permission_read, permission_reads
from app.services.reporting import employee_full_name

router = APIRouter(tags=["leave"])


def _raise(exc: workflow.WorkflowError):
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _statuses(value: Optional[str]) -> Optional[list[str]]:
    if not value:
        return None
    result = []
    for part in value.split(","):
        part = part.strip().upper()
        if part == "PENDING":
            result.extend(PENDING_STATUSES)
        elif part in STATUSES:
            result.append(part)
        elif part:
            raise HTTPException(status_code=400, detail=f"Unknown status '{part}'.")
    return result


# --- leave types ---------------------------------------------------------

@router.get("/leave-types", response_model=list[LeaveTypeRead])
def list_leave_types(db: Session = Depends(get_db)):
    return db.query(LeaveType).order_by(LeaveType.sort_order, LeaveType.id).all()


@router.post("/leave-types", response_model=LeaveTypeRead, status_code=status.HTTP_201_CREATED)
def create_leave_type(payload: LeaveTypeCreate, db: Session = Depends(get_db)):
    if db.query(LeaveType).filter(LeaveType.code == payload.code).first():
        raise HTTPException(status_code=409, detail=f"Leave type code {payload.code} already exists.")
    leave_type = LeaveType(**payload.model_dump())
    db.add(leave_type)
    db.commit()
    db.refresh(leave_type)
    return leave_type


@router.put("/leave-types/{leave_type_id}", response_model=LeaveTypeRead)
def update_leave_type(leave_type_id: int, payload: LeaveTypeUpdate, db: Session = Depends(get_db)):
    leave_type = db.get(LeaveType, leave_type_id)
    if leave_type is None:
        raise HTTPException(status_code=404, detail="Leave type not found.")
    clash = db.query(LeaveType).filter(LeaveType.code == payload.code, LeaveType.id != leave_type_id).first()
    if clash:
        raise HTTPException(status_code=409, detail=f"Leave type code {payload.code} already exists.")
    for field, value in payload.model_dump().items():
        setattr(leave_type, field, value)
    db.commit()
    db.refresh(leave_type)
    return leave_type


@router.delete("/leave-types/{leave_type_id}")
def delete_leave_type(leave_type_id: int, db: Session = Depends(get_db)):
    leave_type = db.get(LeaveType, leave_type_id)
    if leave_type is None:
        raise HTTPException(status_code=404, detail="Leave type not found.")
    in_use = (
        db.query(LeaveApplication.id).filter(LeaveApplication.leave_type_id == leave_type_id).first()
        or db.query(LeaveLedger.id).filter(LeaveLedger.leave_type_id == leave_type_id).first()
    )
    if in_use:
        raise HTTPException(
            status_code=409, detail="This leave type has applications or balance entries; deactivate it instead."
        )
    db.delete(leave_type)
    db.commit()
    return {"success": True}


# --- leave applications --------------------------------------------------

@router.get("/leave-applications", response_model=list[LeaveApplicationRead])
def list_leave_applications(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    employee_id: Optional[int] = None,
    leave_type_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    query = db.query(LeaveApplication)
    statuses = _statuses(status_filter)
    if statuses:
        query = query.filter(LeaveApplication.status.in_(statuses))
    if employee_id is not None:
        query = query.filter(LeaveApplication.employee_id == employee_id)
    if leave_type_id is not None:
        query = query.filter(LeaveApplication.leave_type_id == leave_type_id)
    if date_from is not None:
        query = query.filter(LeaveApplication.to_date >= date_from)
    if date_to is not None:
        query = query.filter(LeaveApplication.from_date <= date_to)
    rows = query.order_by(LeaveApplication.from_date.desc(), LeaveApplication.id.desc()).limit(limit).all()
    return leave_reads(db, rows)


@router.get("/leave-applications/preview", response_model=LeaveDaysPreview)
def preview_leave_days(
    employee_id: int,
    leave_type_id: int,
    from_date: date,
    to_date: date,
    from_half: str = "FULL",
    to_half: str = "FULL",
    db: Session = Depends(get_db),
):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return leave_days_preview(db, employee, leave_type_id, from_date, from_half, to_date, to_half)


def _get_application(db: Session, application_id: int) -> LeaveApplication:
    application = db.get(LeaveApplication, application_id)
    if application is None:
        raise HTTPException(status_code=404, detail="Leave application not found.")
    return application


@router.get("/leave-applications/{application_id}", response_model=LeaveApplicationRead)
def get_leave_application(application_id: int, db: Session = Depends(get_db)):
    return leave_read(db, _get_application(db, application_id))


@router.post("/leave-applications", response_model=LeaveApplicationRead, status_code=status.HTTP_201_CREATED)
def record_leave(payload: LeaveRecordRequest, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    try:
        application, warnings = workflow.record_leave_on_behalf(
            db, hr_user=user, employee_id=payload.employee_id, leave_type_id=payload.leave_type_id,
            from_date=payload.from_date, from_half=payload.from_half, to_date=payload.to_date,
            to_half=payload.to_half, reason=payload.reason, comment=payload.comment,
        )
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(application)
    return leave_read(db, application, warnings=warnings)


def _leave_action(db: Session, application_id: int, user: AdminUser, action) -> LeaveApplicationRead:
    application = _get_application(db, application_id)
    try:
        action(application)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(application)
    return leave_read(db, application)


@router.post("/leave-applications/{application_id}/approve", response_model=LeaveApplicationRead)
def approve_leave(application_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
                  user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _leave_action(db, application_id, user, lambda a: workflow.hr_approve(db, a, user, comment))


@router.post("/leave-applications/{application_id}/reject", response_model=LeaveApplicationRead)
def reject_leave(application_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
                 user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _leave_action(db, application_id, user, lambda a: workflow.hr_reject(db, a, user, comment))


@router.post("/leave-applications/{application_id}/cancel", response_model=LeaveApplicationRead)
def cancel_leave(application_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
                 user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _leave_action(db, application_id, user, lambda a: workflow.cancel_by_hr(db, a, user, comment))


# --- permissions ---------------------------------------------------------

@router.get("/permissions", response_model=list[PermissionRequestRead])
def list_permissions(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    employee_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    query = db.query(PermissionRequest)
    statuses = _statuses(status_filter)
    if statuses:
        query = query.filter(PermissionRequest.status.in_(statuses))
    if employee_id is not None:
        query = query.filter(PermissionRequest.employee_id == employee_id)
    if date_from is not None:
        query = query.filter(PermissionRequest.date >= date_from)
    if date_to is not None:
        query = query.filter(PermissionRequest.date <= date_to)
    rows = query.order_by(PermissionRequest.date.desc(), PermissionRequest.id.desc()).limit(limit).all()
    return permission_reads(db, rows)


@router.get("/permissions/allowance", response_model=PermissionAllowance)
def permission_allowance(employee_id: int, day: date = Query(alias="date"), db: Session = Depends(get_db)):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return workflow.permission_allowance(db, employee, day)


def _get_permission(db: Session, request_id: int) -> PermissionRequest:
    request = db.get(PermissionRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Permission request not found.")
    return request


@router.get("/permissions/{request_id}", response_model=PermissionRequestRead)
def get_permission(request_id: int, db: Session = Depends(get_db)):
    return permission_read(db, _get_permission(db, request_id))


@router.post("/permissions", response_model=PermissionRequestRead, status_code=status.HTTP_201_CREATED)
def record_permission(
    payload: PermissionRecordRequest, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)
):
    try:
        request, warnings = workflow.record_permission_on_behalf(
            db, hr_user=user, employee_id=payload.employee_id, day=payload.date, from_time=payload.from_time,
            to_time=payload.to_time, reason=payload.reason, comment=payload.comment,
            override_limits=payload.override_limits,
        )
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return permission_read(db, request, warnings=warnings)


def _permission_action(db: Session, request_id: int, action) -> PermissionRequestRead:
    request = _get_permission(db, request_id)
    try:
        action(request)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return permission_read(db, request)


@router.post("/permissions/{request_id}/approve", response_model=PermissionRequestRead)
def approve_permission(request_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
                       user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _permission_action(db, request_id, lambda r: workflow.hr_approve(db, r, user, comment))


@router.post("/permissions/{request_id}/reject", response_model=PermissionRequestRead)
def reject_permission(request_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
                      user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _permission_action(db, request_id, lambda r: workflow.hr_reject(db, r, user, comment))


@router.post("/permissions/{request_id}/cancel", response_model=PermissionRequestRead)
def cancel_permission(request_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
                      user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _permission_action(db, request_id, lambda r: workflow.cancel_by_hr(db, r, user, comment))


@router.get("/leave/pending-counts", response_model=PendingCounts)
def pending_counts(db: Session = Depends(get_db)):
    """Requests awaiting a decision (manager or HR), for the tab badges."""
    def count(model):
        return db.query(func.count(model.id)).filter(model.status.in_(PENDING_STATUSES)).scalar() or 0
    return PendingCounts(leave=count(LeaveApplication), permissions=count(PermissionRequest),
                         wfh=count(WfhRequest), regularizations=count(AttendanceRegularization))


# --- balances ------------------------------------------------------------

def balance_rows(
    leave_types: list[LeaveType], summaries: dict, employee_id: int, tier_labels: Optional[dict] = None,
    months: Optional[dict] = None,
) -> list[BalanceRow]:
    rows = []
    for leave_type in leave_types:
        summary = summaries.get((employee_id, leave_type.id)) or balance_service.empty_summary()
        rows.append(BalanceRow(
            leave_type_id=leave_type.id, leave_type_code=leave_type.code, leave_type_name=leave_type.name,
            tracks_balance=leave_type.tracks_balance, tier_label=(tier_labels or {}).get((employee_id, leave_type.id)),
            **summary, **(months or {}).get((employee_id, leave_type.id), {}),
        ))
    return rows


def employee_balances(db: Session, employees: list[Employee], leave_types: list[LeaveType], year: int,
                      month: Optional[int] = None) -> list[EmployeeBalances]:
    """Year and month balances (month: calendar month within the leave year,
    default the current one) for the employees."""
    try:
        first = balance_service.selected_month(db, year, month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    summaries = balance_service.balance_summaries(db, year, [e.id for e in employees])
    labels = balance_service.tier_labels(leave_types, employees, first)
    months = balance_service.month_summaries(db, year, first, leave_types, employees, summaries)
    return [
        EmployeeBalances(
            employee_id=e.id, employee_code=e.employee_code, employee_name=employee_full_name(e), year=year,
            month=first.strftime("%Y-%m"), balances=balance_rows(leave_types, summaries, e.id, labels, months),
        )
        for e in employees
    ]


def _paid_types(db: Session) -> list[LeaveType]:
    return (
        db.query(LeaveType).filter(LeaveType.is_active.is_(True), LeaveType.is_paid.is_(True))
        .order_by(LeaveType.sort_order, LeaveType.id).all()
    )


@router.get("/leave-balances", response_model=list[EmployeeBalances])
def list_balances(
    year: Optional[int] = None,
    month: Optional[int] = Query(default=None, ge=1, le=12),
    employee_id: Optional[int] = None,
    search: Optional[str] = None,
    include_inactive: bool = False,
    db: Session = Depends(get_db),
):
    year = year or balance_service.leave_year_for(db, date.today())
    query = db.query(Employee)
    if not include_inactive:
        query = query.filter(Employee.status == "Active")
    if employee_id is not None:
        query = query.filter(Employee.id == employee_id)
    if search:
        like = f"%{search.strip()}%"
        query = query.filter(or_(Employee.employee_code.ilike(like), Employee.first_name.ilike(like),
                                 Employee.last_name.ilike(like)))
    employees = query.order_by(Employee.employee_code).all()
    return employee_balances(db, employees, _paid_types(db), year, month)


@router.get("/leave-balances/ledger", response_model=list[LedgerEntryRead])
def list_ledger(employee_id: int, year: Optional[int] = None, leave_type_id: Optional[int] = None,
                db: Session = Depends(get_db)):
    query = db.query(LeaveLedger).filter(LeaveLedger.employee_id == employee_id)
    if year is not None:
        query = query.filter(LeaveLedger.leave_year == year)
    if leave_type_id is not None:
        query = query.filter(LeaveLedger.leave_type_id == leave_type_id)
    return [
        LedgerEntryRead(
            id=r.id, employee_id=r.employee_id, leave_type_id=r.leave_type_id,
            leave_type_code=r.leave_type.code if r.leave_type else None, leave_year=r.leave_year,
            entry_type=r.entry_type, days=r.days, reference_type=r.reference_type, reference_id=r.reference_id,
            note=r.note, created_at=r.created_at,
        )
        for r in query.order_by(LeaveLedger.created_at, LeaveLedger.id).all()
    ]


@router.post("/leave-balances/adjust", response_model=EmployeeBalances)
def adjust_balance(payload: BalanceAdjustRequest, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    employee = db.get(Employee, payload.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    leave_type = db.get(LeaveType, payload.leave_type_id)
    if leave_type is None:
        raise HTTPException(status_code=404, detail="Leave type not found.")
    if not leave_type.tracks_balance:
        raise HTTPException(status_code=400, detail=f"{leave_type.name} is unpaid and has no balance to adjust.")
    year = payload.year or balance_service.leave_year_for(db, date.today())
    balance_service.adjust(db, employee.id, leave_type.id, year, payload.days, payload.note.strip(),
                           created_by=user.id, entry_type=payload.entry_type)
    db.commit()
    return employee_balances(db, [employee], _paid_types(db), year)[0]


@router.post("/leave-balances/credit-run", response_model=CreditRunResult)
def credit_run(payload: CreditRunRequest, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    result = balance_service.credit_year(
        db, payload.year, leave_type_ids=payload.leave_type_ids, as_of=payload.as_of, created_by=user.id
    )
    db.commit()
    return result


@router.post("/leave-balances/credit-month", response_model=MonthlyCreditResult)
def credit_month_run(payload: CreditMonthRequest, db: Session = Depends(get_db),
                     user: AdminUser = Depends(require_hr)):
    """Monthly credit (and previous month's lapse) for the leave types
    credited monthly by length of service. Safe to re-run."""
    try:
        result = balance_service.credit_month(db, payload.year, payload.month, created_by=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result


@router.post("/leave-balances/credit-catch-up", response_model=MonthlyCreditResult)
def credit_catch_up_run(payload: Optional[CreditCatchUpRequest] = None, db: Session = Depends(get_db),
                        user: AdminUser = Depends(require_hr)):
    """Every month of the leave year up to the current month, in order."""
    try:
        result = balance_service.credit_catch_up(db, payload.year if payload else None, created_by=user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.commit()
    return result
