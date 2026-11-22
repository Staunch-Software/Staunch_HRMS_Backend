"""Employee self-service leave and permission endpoints, plus the
department-manager approval inbox. Scoped to the caller's employee record."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.api.v1.leave_admin import employee_balances
from app.api.v1.me import get_current_employee
from app.core.db import get_db
from app.core.security import require_employee_user
from app.models.employee import Employee
from app.models.leave import (
    STATUS_PENDING_MANAGER,
    LeaveApplication,
    LeaveType,
    PermissionRequest,
)
from app.models.user import AdminUser
from app.schemas.leave import (
    ActionComment,
    EmployeeBalances,
    HolidayBrief,
    LeaveApplicationRead,
    LeaveApplyRequest,
    LeaveDaysPreview,
    LeaveRules,
    LeaveTypeRead,
    PermissionAllowance,
    PermissionApplyRequest,
    PermissionRequestRead,
)
from app.services.approval_settings import chain_for
from app.services.leave import balance as balance_service
from app.services.leave import workflow
from app.services.leave.calendar import holidays_between, policy_for
from app.services.leave.preview import leave_days_preview
from app.services.leave.serialize import leave_read, leave_reads, permission_read, permission_reads
from app.models.wfh import WfhRequest
from app.services.leave.serialize import regularization_reads, wfh_reads
from app.models.regularization import AttendanceRegularization
from app.schemas.regularization import ApprovalsInboxAll
from app.services.reporting import department_member_ids, employee_full_name

router = APIRouter(prefix="/me", tags=["self-service"], dependencies=[Depends(require_employee_user)])


def _raise(exc: workflow.WorkflowError):
    raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


def _active_types(db: Session) -> list[LeaveType]:
    return (
        db.query(LeaveType).filter(LeaveType.is_active.is_(True))
        .order_by(LeaveType.sort_order, LeaveType.id).all()
    )


@router.get("/leave-types", response_model=list[LeaveTypeRead])
def my_leave_types(db: Session = Depends(get_db)):
    return _active_types(db)


@router.get("/leave-rules", response_model=LeaveRules)
def my_leave_rules(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    policy = policy_for(db, employee)
    manager = workflow.approving_manager(db, employee, "LEAVE")
    permission_manager = workflow.approving_manager(db, employee, "PERMISSION")
    return LeaveRules(
        min_notice_days=policy.min_notice_days,
        leave_year_start_month=policy.leave_year_start_month,
        permission_monthly_minutes=policy.permission_monthly_minutes,
        permission_max_per_month=policy.permission_max_per_month,
        permission_max_minutes_each=policy.permission_max_minutes_each,
        shift_start=policy.shift_start,
        required_minutes=policy.required_minutes,
        notice_start_date=employee.notice_start_date,
        has_manager=manager is not None,
        manager_name=employee_full_name(manager),
        permission_has_manager=permission_manager is not None,
        permission_manager_name=employee_full_name(permission_manager),
        leave_chain=chain_for(db, "LEAVE"),
        permission_chain=chain_for(db, "PERMISSION"),
    )


@router.get("/balances", response_model=EmployeeBalances)
def my_balances(
    year: Optional[int] = None,
    month: Optional[int] = Query(default=None, ge=1, le=12),
    employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    year = year or balance_service.leave_year_for(db, date.today())
    leave_types = [t for t in _active_types(db) if t.tracks_balance]
    return employee_balances(db, [employee], leave_types, year, month)[0]


@router.get("/holidays", response_model=list[HolidayBrief])
def my_holidays(
    year: Optional[int] = None, employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)
):
    year = year or date.today().year
    holidays = holidays_between(db, date(year, 1, 1), date(year, 12, 31), employee.work_location_id)
    return [HolidayBrief(date=h.date, name=h.name) for _, h in sorted(holidays.items())]


@router.get("/leave-days-preview", response_model=LeaveDaysPreview)
def my_leave_days_preview(
    leave_type_id: int = Query(alias="type"),
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
    from_half: str = "FULL",
    to_half: str = "FULL",
    employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    return leave_days_preview(db, employee, leave_type_id, from_date, from_half, to_date, to_half)


# --- my leave ------------------------------------------------------------

@router.get("/leave-applications", response_model=list[LeaveApplicationRead])
def my_leave_applications(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    rows = (
        db.query(LeaveApplication).filter(LeaveApplication.employee_id == employee.id)
        .order_by(LeaveApplication.from_date.desc(), LeaveApplication.id.desc()).all()
    )
    return leave_reads(db, rows)


@router.post("/leave-applications", response_model=LeaveApplicationRead, status_code=status.HTTP_201_CREATED)
def apply_my_leave(
    payload: LeaveApplyRequest,
    employee: Employee = Depends(get_current_employee),
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    try:
        application = workflow.apply_leave(
            db, employee=employee, user=user, leave_type_id=payload.leave_type_id, from_date=payload.from_date,
            from_half=payload.from_half, to_date=payload.to_date, to_half=payload.to_half, reason=payload.reason,
        )
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(application)
    return leave_read(db, application)


@router.post("/leave-applications/{application_id}/cancel", response_model=LeaveApplicationRead)
def cancel_my_leave(
    application_id: int,
    payload: Optional[ActionComment] = None,
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    application = db.get(LeaveApplication, application_id)
    if application is None or application.employee_id != user.employee_id:
        raise HTTPException(status_code=404, detail="Leave application not found.")
    try:
        workflow.cancel_by_employee(db, application, user, payload.comment if payload else None)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(application)
    return leave_read(db, application)


# --- my permissions ------------------------------------------------------

@router.get("/permissions", response_model=list[PermissionRequestRead])
def my_permissions(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    rows = (
        db.query(PermissionRequest).filter(PermissionRequest.employee_id == employee.id)
        .order_by(PermissionRequest.date.desc(), PermissionRequest.id.desc()).all()
    )
    return permission_reads(db, rows)


@router.get("/permission-allowance", response_model=PermissionAllowance)
def my_permission_allowance(
    day: Optional[date] = Query(default=None, alias="date"),
    employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    return workflow.permission_allowance(db, employee, day or date.today())


@router.post("/permissions", response_model=PermissionRequestRead, status_code=status.HTTP_201_CREATED)
def apply_my_permission(
    payload: PermissionApplyRequest,
    employee: Employee = Depends(get_current_employee),
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    try:
        request = workflow.apply_permission(
            db, employee=employee, user=user, day=payload.date, from_time=payload.from_time,
            to_time=payload.to_time, reason=payload.reason,
        )
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return permission_read(db, request)


@router.post("/permissions/{request_id}/cancel", response_model=PermissionRequestRead)
def cancel_my_permission(
    request_id: int,
    payload: Optional[ActionComment] = None,
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    request = db.get(PermissionRequest, request_id)
    if request is None or request.employee_id != user.employee_id:
        raise HTTPException(status_code=404, detail="Permission request not found.")
    try:
        workflow.cancel_by_employee(db, request, user, payload.comment if payload else None)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return permission_read(db, request)


# --- manager approvals ---------------------------------------------------

def _awaiting_me(db: Session, user: AdminUser, model, report_ids: list[int], *order):
    """PENDING_MANAGER requests of the caller's department members, plus
    those routed to the caller in person (reporting-manager fallback) that
    the caller can still act on."""
    mine = model.manager_id == user.employee_id
    who = or_(model.employee_id.in_(report_ids), mine) if report_ids else mine
    rows = db.query(model).filter(who, model.status == STATUS_PENDING_MANAGER).order_by(*order).all()
    return [r for r in rows if r.employee_id in report_ids or workflow.can_manager_act(db, r, user)]


@router.get("/approvals", response_model=ApprovalsInboxAll)
def my_approvals(user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    report_ids = department_member_ids(db, user.employee_id)
    leave = _awaiting_me(db, user, LeaveApplication, report_ids, LeaveApplication.from_date, LeaveApplication.id)
    permissions = _awaiting_me(db, user, PermissionRequest, report_ids, PermissionRequest.date, PermissionRequest.id)
    wfh = _awaiting_me(db, user, WfhRequest, report_ids, WfhRequest.from_date, WfhRequest.id)
    punches = _awaiting_me(db, user, AttendanceRegularization, report_ids, AttendanceRegularization.date,
                           AttendanceRegularization.id)
    return ApprovalsInboxAll(leave=leave_reads(db, leave), permissions=permission_reads(db, permissions),
                             wfh=wfh_reads(db, wfh), regularizations=regularization_reads(db, punches))


def _manager_action(db: Session, model, request_id: int, user: AdminUser, approve: bool,
                    payload: Optional[ActionComment]):
    request = db.get(model, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Request not found.")
    try:
        workflow.manager_decide(db, request, user, approve, payload.comment if payload else None)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return request


@router.post("/approvals/leave/{application_id}/approve", response_model=LeaveApplicationRead)
def manager_approve_leave(application_id: int, payload: Optional[ActionComment] = None,
                          user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    return leave_read(db, _manager_action(db, LeaveApplication, application_id, user, True, payload))


@router.post("/approvals/leave/{application_id}/reject", response_model=LeaveApplicationRead)
def manager_reject_leave(application_id: int, payload: Optional[ActionComment] = None,
                         user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    return leave_read(db, _manager_action(db, LeaveApplication, application_id, user, False, payload))


@router.post("/approvals/permission/{request_id}/approve", response_model=PermissionRequestRead)
def manager_approve_permission(request_id: int, payload: Optional[ActionComment] = None,
                               user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    return permission_read(db, _manager_action(db, PermissionRequest, request_id, user, True, payload))


@router.post("/approvals/permission/{request_id}/reject", response_model=PermissionRequestRead)
def manager_reject_permission(request_id: int, payload: Optional[ActionComment] = None,
                              user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    return permission_read(db, _manager_action(db, PermissionRequest, request_id, user, False, payload))
