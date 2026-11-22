"""Leave, permission and WFH request workflow.

The approval chain is configurable per request type (Settings > Approval
Workflow, services/approval_settings.py): MANAGER_HR (default: the
department manager, then HR), HR_ONLY, or MANAGER_ONLY (the manager's
approval is final). The manager step is skipped (straight to HR) when the
employee has no department, the department has no manager, the manager is
the employee, or the manager is inactive or has no active login; with the
REPORTING_MANAGER fallback the employee's reporting manager is tried next.
HR approves or rejects only after the manager step (PENDING_HR), records requests on behalf of an
employee (approved immediately, may be backdated) and cancel approved
leave, which reverses the ledger debit.

Reusable approval interface (any request model with the _ApprovalFields
columns plus employee_id / employee, e.g. a missed-punch request):
  submit_routing(db, employee, request_type) -> (status, manager or None)
  approving_manager(db, employee, request_type="LEAVE")
  can_manager_act(db, request, user)
  next_status_after_manager(db, request_type, approve)
  manager_decide / hr_approve / hr_reject (status and audit fields)
  register_approval_hooks(request_type, check=None, apply=None): extra work
    when a request of that type is finally approved (by HR, or by the
    manager under MANAGER_ONLY)
request_type_of(request) reads a class attribute `approval_request_type`
when the model defines one.
"""
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional, Union

from sqlalchemy import extract
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import (
    ACTIVE_STATUSES,
    HALF_FULL,
    PENDING_STATUSES,
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_PENDING_HR,
    STATUS_PENDING_MANAGER,
    STATUS_REJECTED,
    LeaveApplication,
    LeaveType,
    PermissionRequest,
)
from app.models.org import Department
from app.models.regularization import AttendanceRegularization
from app.models.user import AdminUser
from app.models.wfh import WfhRequest
from app.services import approval_settings as approvals
from app.services.approval_settings import REQUEST_LEAVE, REQUEST_PERMISSION, REQUEST_REGULARIZATION, REQUEST_WFH
from app.services.leave import balance as balance_service
from app.services.leave import notify
from app.services.leave.calendar import KIND_HOLIDAY, KIND_WORK, day_kinds, policy_for
from app.services.leave.days import LeaveDaysError, half_slots, leave_day_breakdown, validate_halves

ZERO = Decimal("0")
Request = Union[LeaveApplication, PermissionRequest]


class WorkflowError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(timezone.utc)


def fmt_days(value: Decimal) -> str:
    value = Decimal(value).normalize()
    return format(value, "f")


def fmt_date(day: date) -> str:
    return day.strftime("%d %b %Y")


def _is_active(employee: Optional[Employee]) -> bool:
    return employee is not None and (employee.status or "").strip().lower() == "active"


def department_manager(employee: Optional[Employee]) -> Optional[Employee]:
    """The current manager of the employee's department, if any."""
    department = employee.department if employee is not None else None
    return department.manager if department is not None else None


def request_type_of(request) -> str:
    explicit = getattr(request, "approval_request_type", None)
    if explicit:
        return explicit
    if isinstance(request, LeaveApplication):
        return REQUEST_LEAVE
    if isinstance(request, WfhRequest):
        return REQUEST_WFH
    if isinstance(request, PermissionRequest):
        return REQUEST_PERMISSION
    raise ValueError(f"Unknown request {type(request).__name__}.")


def has_active_login(db: Session, employee_id: Optional[int]) -> bool:
    if employee_id is None:
        return False
    return db.query(AdminUser.id).filter(
        AdminUser.employee_id == employee_id, AdminUser.is_active.is_(True)
    ).first() is not None


def _eligible(db: Session, employee: Employee, candidate: Optional[Employee]) -> bool:
    return (candidate is not None and candidate.id != employee.id and _is_active(candidate)
            and has_active_login(db, candidate.id))


def approving_manager(db: Session, employee: Employee, request_type: str = REQUEST_LEAVE) -> Optional[Employee]:
    """The manager who acts on the first step, or None when the request
    goes straight to HR: the chain for request_type is HR_ONLY, or there is
    no eligible manager (no department or manager, the manager is the
    employee, inactive, or has no active login). With the REPORTING_MANAGER
    fallback an ineligible department manager is replaced by the employee's
    reporting manager when that one is eligible."""
    settings = approvals.get_settings(db)
    chain = getattr(settings, approvals.CHAIN_FIELDS[request_type])
    if not approvals.chain_includes_manager(chain):
        return None
    manager = department_manager(employee)
    if _eligible(db, employee, manager):
        return manager
    if settings.no_manager_fallback == approvals.FALLBACK_REPORTING_MANAGER and employee.reporting_manager_id:
        reporting = db.get(Employee, employee.reporting_manager_id)
        if _eligible(db, employee, reporting):
            return reporting
    return None


def submit_routing(db: Session, employee: Employee, request_type: str) -> tuple[str, Optional[Employee]]:
    """(initial status, manager) for a request the employee submits."""
    manager = approving_manager(db, employee, request_type)
    return (STATUS_PENDING_MANAGER if manager else STATUS_PENDING_HR), manager


def next_status_after_manager(db: Session, request_type: str, approve: bool) -> str:
    if not approve:
        return STATUS_REJECTED
    return STATUS_APPROVED if approvals.manager_is_final(approvals.chain_for(db, request_type)) else STATUS_PENDING_HR


def can_manager_act(db: Session, request, user: AdminUser) -> bool:
    """The current department manager, or the manager the request would be
    routed to now (e.g. the reporting manager under the fallback)."""
    if user.employee_id is None or request.employee_id == user.employee_id:
        return False
    employee = request.employee
    manager = department_manager(employee)
    if manager is not None and manager.id == user.employee_id:
        return True
    current = approving_manager(db, employee, request_type_of(request))
    return current is not None and current.id == user.employee_id


# request_type -> (check(db, request), apply(db, request, user)) run on final approval.
_APPROVAL_HOOKS: dict[str, tuple[Optional[Callable], Optional[Callable]]] = {}


def register_approval_hooks(request_type: str, check: Optional[Callable] = None,
                            apply: Optional[Callable] = None) -> None:
    _APPROVAL_HOOKS[request_type] = (check, apply)


def _manager_id(employee: Employee) -> Optional[int]:
    manager = department_manager(employee)
    return manager.id if manager is not None and manager.id != employee.id else None


def _get_employee(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise WorkflowError("Employee not found.", 404)
    return employee


def _get_leave_type(db: Session, leave_type_id: int) -> LeaveType:
    leave_type = db.get(LeaveType, leave_type_id)
    if leave_type is None or not leave_type.is_active:
        raise WorkflowError("Leave type not found or inactive.", 404 if leave_type is None else 400)
    return leave_type


def _not_self(user: AdminUser, employee_id: int, action: str) -> None:
    if user.employee_id is not None and user.employee_id == employee_id:
        raise WorkflowError(f"You cannot {action} your own request; another HR user must do it.", 403)


# --- leave ---------------------------------------------------------------

def overlapping_leave(
    db: Session, employee_id: int, from_date: date, from_half: str, to_date: date, to_half: str,
    exclude_id: Optional[int] = None,
) -> Optional[LeaveApplication]:
    candidates = db.query(LeaveApplication).filter(
        LeaveApplication.employee_id == employee_id,
        LeaveApplication.status.in_(ACTIVE_STATUSES),
        LeaveApplication.from_date <= to_date,
        LeaveApplication.to_date >= from_date,
    )
    wanted = half_slots(from_date, from_half, to_date, to_half)
    for other in candidates:
        if other.id == exclude_id:
            continue
        if wanted & half_slots(other.from_date, other.from_half, other.to_date, other.to_half):
            return other
    return None


def _prepare_leave(
    db: Session, employee: Employee, leave_type: LeaveType, from_date: date, from_half: str, to_date: date,
    to_half: str,
):
    try:
        from_half, to_half = validate_halves(from_date, from_half, to_date, to_half)
    except LeaveDaysError as exc:
        raise WorkflowError(str(exc)) from exc
    if not leave_type.allow_half_day and (from_half != HALF_FULL or to_half != HALF_FULL):
        raise WorkflowError(f"{leave_type.name} cannot be taken as a half day.")
    if balance_service.leave_year_for(db, from_date) != balance_service.leave_year_for(db, to_date):
        raise WorkflowError("A leave cannot span two leave years; apply separately for each year.")
    breakdown = leave_day_breakdown(db, employee, leave_type, from_date, from_half, to_date, to_half)
    days = sum((d.counted for d in breakdown), ZERO)
    if days <= 0:
        raise WorkflowError("The selected dates have no working days, so no leave is needed.")
    other = overlapping_leave(db, employee.id, from_date, from_half, to_date, to_half)
    if other is not None:
        raise WorkflowError(
            f"This overlaps an existing leave request ({fmt_date(other.from_date)} to "
            f"{fmt_date(other.to_date)}, {other.status.replace('_', ' ').lower()})."
        )
    return from_half, to_half, days, breakdown


def _check_balance(
    db: Session, employee_id: int, leave_type: LeaveType, from_date: date, days: Decimal,
    include_pending: bool, exclude_id: Optional[int] = None,
) -> None:
    if not leave_type.tracks_balance or leave_type.allow_negative_balance:
        return
    if balance_service.is_tiered(leave_type):
        available = balance_service.available_on(
            db, employee_id, leave_type.id, from_date, include_pending=include_pending, exclude_id=exclude_id)
    else:
        year = balance_service.leave_year_for(db, from_date)
        available = balance_service.balance(db, employee_id, leave_type.id, year)
        if include_pending:
            available -= balance_service.pending_days(db, employee_id, leave_type.id, year, exclude_id=exclude_id)
    if days > available:
        raise WorkflowError(
            f"Insufficient {leave_type.name} balance: {fmt_days(max(available, ZERO))} day(s) available, "
            f"{fmt_days(days)} requested."
        )


def _chargeable(breakdown, employee: Employee, leave_type: LeaveType) -> Decimal:
    notice = employee.notice_start_date
    if notice is None or leave_type.allowed_during_notice:
        return sum((d.counted for d in breakdown), ZERO)
    return sum((d.counted for d in breakdown if d.date < notice), ZERO)


def chargeable_days(db: Session, application: LeaveApplication) -> Decimal:
    """Days to debit from the balance: all counted days, except days on or
    after the notice start date for a type not allowed during notice (those
    are treated as Loss of Pay by attendance processing)."""
    employee = application.employee
    leave_type = application.leave_type
    notice = employee.notice_start_date
    if notice is None or leave_type.allowed_during_notice or application.to_date < notice:
        return Decimal(application.days)
    breakdown = leave_day_breakdown(
        db, employee, leave_type, application.from_date, application.from_half,
        application.to_date, application.to_half,
    )
    return _chargeable(breakdown, employee, leave_type)


def apply_leave(
    db: Session, *, employee: Employee, user: AdminUser, leave_type_id: int, from_date: date, from_half: str,
    to_date: date, to_half: str, reason: Optional[str], today: Optional[date] = None,
) -> LeaveApplication:
    today = today or date.today()
    if not _is_active(employee):
        raise WorkflowError("Only active employees can apply for leave.", 403)
    leave_type = _get_leave_type(db, leave_type_id)
    if from_date < today:
        raise WorkflowError("Backdated leave cannot be applied. Contact HR to record it.")
    policy = policy_for(db, employee)
    notice_days = policy.min_notice_days or 0
    earliest = today + timedelta(days=notice_days)
    if from_date < earliest:
        raise WorkflowError(
            f"Leave must be applied at least {notice_days} day(s) in advance (earliest start date "
            f"{fmt_date(earliest)}). For an emergency, contact HR."
        )
    notice = employee.notice_start_date
    if notice and not leave_type.allowed_during_notice and to_date >= notice:
        raise WorkflowError(
            f"{leave_type.name} is not permitted during the notice period (from {fmt_date(notice)})."
        )
    from_half, to_half, days, _ = _prepare_leave(db, employee, leave_type, from_date, from_half, to_date, to_half)
    _check_balance(db, employee.id, leave_type, from_date, days, include_pending=True)

    initial, manager = submit_routing(db, employee, REQUEST_LEAVE)
    application = LeaveApplication(
        employee_id=employee.id, leave_type_id=leave_type.id, from_date=from_date, to_date=to_date,
        from_half=from_half, to_half=to_half, days=days, reason=(reason or "").strip() or None,
        status=initial, applied_by_user_id=user.id,
        recorded_by_hr=False, manager_id=manager.id if manager else None,
    )
    db.add(application)
    db.flush()
    notify.submitted(db, application, user)
    return application


def record_leave_on_behalf(
    db: Session, *, hr_user: AdminUser, employee_id: int, leave_type_id: int, from_date: date, from_half: str,
    to_date: date, to_half: str, reason: Optional[str], comment: Optional[str] = None,
    today: Optional[date] = None,
) -> tuple[LeaveApplication, list[str]]:
    """HR records a leave that is approved immediately. Backdating, the
    minimum notice and the notice-period rule produce warnings, not errors."""
    today = today or date.today()
    employee = _get_employee(db, employee_id)
    _not_self(hr_user, employee.id, "record")
    leave_type = _get_leave_type(db, leave_type_id)
    from_half, to_half, days, breakdown = _prepare_leave(
        db, employee, leave_type, from_date, from_half, to_date, to_half
    )
    chargeable = _chargeable(breakdown, employee, leave_type)
    _check_balance(db, employee.id, leave_type, from_date, chargeable, include_pending=False)

    warnings = []
    if not _is_active(employee):
        warnings.append(f"The employee's status is {employee.status}.")
    if from_date < today:
        warnings.append("Backdated leave recorded by HR.")
    else:
        notice_days = policy_for(db, employee).min_notice_days or 0
        if from_date < today + timedelta(days=notice_days):
            warnings.append(f"Recorded with less than the {notice_days}-day minimum notice.")
    notice = employee.notice_start_date
    if notice and not leave_type.allowed_during_notice and to_date >= notice:
        warnings.append(
            f"{leave_type.name} is not permitted during the notice period (from {fmt_date(notice)}); days from "
            "that date are not deducted from the balance and will be treated as Loss of Pay."
        )

    application = LeaveApplication(
        employee_id=employee.id, leave_type_id=leave_type.id, from_date=from_date, to_date=to_date,
        from_half=from_half, to_half=to_half, days=days, reason=(reason or "").strip() or None,
        status=STATUS_APPROVED, applied_by_user_id=hr_user.id, recorded_by_hr=True,
        manager_id=_manager_id(employee), hr_action_by=hr_user.id, hr_action_at=_now(),
        hr_comment=(comment or "").strip() or None,
    )
    db.add(application)
    db.flush()
    balance_service.debit_for_application(db, application, days=chargeable, created_by=hr_user.id)
    notify.recorded(db, application, hr_user)
    return application, warnings


# --- shared approval steps -----------------------------------------------

def _final_check(db: Session, request) -> Optional[Decimal]:
    """Validates a final approval; returns the leave days to debit."""
    chargeable = None
    if isinstance(request, LeaveApplication):
        chargeable = chargeable_days(db, request)
        _check_balance(db, request.employee_id, request.leave_type, request.from_date, chargeable,
                       include_pending=False)
    check, _ = _APPROVAL_HOOKS.get(request_type_of(request), (None, None))
    if check is not None:
        check(db, request)
    return chargeable


def _final_apply(db: Session, request, user: AdminUser, chargeable: Optional[Decimal]) -> None:
    if chargeable is not None:
        balance_service.debit_for_application(db, request, days=chargeable, created_by=user.id)
    _, apply = _APPROVAL_HOOKS.get(request_type_of(request), (None, None))
    if apply is not None:
        apply(db, request, user)


def manager_decide(db: Session, request: Request, user: AdminUser, approve: bool, comment: Optional[str]) -> Request:
    """Manager step. Under MANAGER_ONLY an approval is final (APPROVED),
    otherwise it moves the request to PENDING_HR."""
    if not can_manager_act(db, request, user):
        raise WorkflowError("Only the manager of the employee's department can act on this request.", 403)
    if request.status != STATUS_PENDING_MANAGER:
        raise WorkflowError(f"This request is not awaiting manager approval (status {request.status}).", 409)
    new_status = next_status_after_manager(db, request_type_of(request), approve)
    chargeable = _final_check(db, request) if new_status == STATUS_APPROVED else None
    request.manager_action_by = user.id
    request.manager_action_at = _now()
    request.manager_comment = (comment or "").strip() or None
    request.status = new_status
    db.flush()
    if new_status == STATUS_APPROVED:
        _final_apply(db, request, user, chargeable)
        notify.manager_final_approved(db, request, user)
    else:
        notify.manager_decided(db, request, user, approve)
    return request


def _require_hr_step(request: Request, action: str) -> None:
    """HR decides only once the request reached the HR step: a request still
    with the manager must be decided by the manager first."""
    if request.status == STATUS_PENDING_MANAGER:
        raise WorkflowError(f"This request is waiting for the manager's approval. HR can {action} it after the "
                            "manager approves.", 409)
    if request.status != STATUS_PENDING_HR:
        raise WorkflowError(f"Only requests pending with HR can be {action}d (status {request.status}).", 409)


def hr_approve(db: Session, request: Request, user: AdminUser, comment: Optional[str]) -> Request:
    _not_self(user, request.employee_id, "approve")
    _require_hr_step(request, "approve")
    chargeable = _final_check(db, request)
    request.status = STATUS_APPROVED
    request.hr_action_by = user.id
    request.hr_action_at = _now()
    request.hr_comment = (comment or "").strip() or None
    db.flush()
    _final_apply(db, request, user, chargeable)
    notify.hr_decided(db, request, user, True)
    return request


def hr_reject(db: Session, request: Request, user: AdminUser, comment: Optional[str]) -> Request:
    _require_hr_step(request, "reject")
    request.status = STATUS_REJECTED
    request.hr_action_by = user.id
    request.hr_action_at = _now()
    request.hr_comment = (comment or "").strip() or None
    db.flush()
    notify.hr_decided(db, request, user, False)
    return request


def cancel_by_employee(db: Session, request: Request, user: AdminUser, comment: Optional[str] = None) -> Request:
    if request.employee_id != user.employee_id:
        raise WorkflowError("Request not found.", 404)
    if request.status not in PENDING_STATUSES:
        raise WorkflowError("Only pending requests can be cancelled. Contact HR to cancel an approved one.", 409)
    previous_status = request.status
    _cancel(db, request, user, comment)
    notify.cancelled_by_employee(db, request, user, previous_status)
    return request


def cancel_by_hr(db: Session, request: Request, user: AdminUser, comment: Optional[str] = None) -> Request:
    if request.status not in ACTIVE_STATUSES:
        raise WorkflowError(f"This request is already {request.status.lower()}.", 409)
    was_approved = request.status == STATUS_APPROVED
    _cancel(db, request, user, comment)
    if was_approved and isinstance(request, LeaveApplication):
        balance_service.reverse_for_application(db, request, created_by=user.id, note="Approved leave cancelled by HR")
    notify.cancelled_by_hr(db, request, user)
    return request


def _cancel(db: Session, request: Request, user: AdminUser, comment: Optional[str]) -> Request:
    request.status = STATUS_CANCELLED
    request.cancelled_by = user.id
    request.cancelled_at = _now()
    request.cancel_comment = (comment or "").strip() or None
    db.flush()
    return request


# --- permission ----------------------------------------------------------

def minutes_between(from_time: time, to_time: time) -> int:
    return (to_time.hour * 60 + to_time.minute) - (from_time.hour * 60 + from_time.minute)


def permission_usage(
    db: Session, employee_id: int, day: date, exclude_id: Optional[int] = None
) -> tuple[int, int]:
    """(count, minutes) of pending + approved permissions in day's month."""
    query = db.query(PermissionRequest).filter(
        PermissionRequest.employee_id == employee_id,
        PermissionRequest.status.in_(ACTIVE_STATUSES),
        extract("year", PermissionRequest.date) == day.year,
        extract("month", PermissionRequest.date) == day.month,
    )
    rows = [r for r in query if r.id != exclude_id]
    return len(rows), sum(r.minutes for r in rows)


def permission_allowance(db: Session, employee: Employee, day: date) -> dict:
    policy = policy_for(db, employee)
    count, minutes = permission_usage(db, employee.id, day)
    return {
        "month": day.strftime("%Y-%m"),
        "monthly_minutes": policy.permission_monthly_minutes,
        "max_per_month": policy.permission_max_per_month,
        "max_minutes_each": policy.permission_max_minutes_each,
        "used_count": count,
        "used_minutes": minutes,
        "remaining_count": max(policy.permission_max_per_month - count, 0),
        "remaining_minutes": max(policy.permission_monthly_minutes - minutes, 0),
    }


def permission_limit_errors(db: Session, employee: Employee, day: date, minutes: int,
                            exclude_id: Optional[int] = None) -> list[str]:
    policy = policy_for(db, employee)
    count, used = permission_usage(db, employee.id, day, exclude_id)
    errors = []
    if minutes > policy.permission_max_minutes_each:
        errors.append(f"A permission can be at most {policy.permission_max_minutes_each} minutes.")
    if count >= policy.permission_max_per_month:
        errors.append(f"Only {policy.permission_max_per_month} permissions are allowed per month "
                      f"({count} already requested for {day.strftime('%b %Y')}).")
    if used + minutes > policy.permission_monthly_minutes:
        errors.append(f"Monthly permission limit is {policy.permission_monthly_minutes} minutes; "
                      f"{used} already used or pending, {max(policy.permission_monthly_minutes - used, 0)} left.")
    return errors


def _prepare_permission(db: Session, employee: Employee, day: date, from_time: time, to_time: time) -> int:
    minutes = minutes_between(from_time, to_time)
    if minutes <= 0:
        raise WorkflowError("The end time must be after the start time.")
    others = db.query(PermissionRequest).filter(
        PermissionRequest.employee_id == employee.id,
        PermissionRequest.date == day,
        PermissionRequest.status.in_(ACTIVE_STATUSES),
    )
    for other in others:
        if from_time < other.to_time and other.from_time < to_time:
            raise WorkflowError(
                f"This overlaps another permission on {fmt_date(day)} "
                f"({other.from_time.strftime('%H:%M')}-{other.to_time.strftime('%H:%M')})."
            )
    leave = overlapping_leave(db, employee.id, day, HALF_FULL, day, HALF_FULL)
    if leave and half_slots(leave.from_date, leave.from_half, leave.to_date, leave.to_half) >= {(day, 1), (day, 2)}:
        raise WorkflowError(f"There is already a full-day leave request on {fmt_date(day)}.")
    return minutes


def _day_kind_label(db: Session, employee: Employee, day: date) -> Optional[str]:
    kind, holiday = day_kinds(db, employee, day, day)[day]
    if kind == KIND_WORK:
        return None
    if kind == KIND_HOLIDAY:
        return f"{fmt_date(day)} is a holiday ({holiday.name})"
    return f"{fmt_date(day)} is a weekly off"


def apply_permission(
    db: Session, *, employee: Employee, user: AdminUser, day: date, from_time: time, to_time: time,
    reason: Optional[str], today: Optional[date] = None,
) -> PermissionRequest:
    today = today or date.today()
    if not _is_active(employee):
        raise WorkflowError("Only active employees can request permission.", 403)
    if day < today:
        raise WorkflowError(
            "Permission must be requested before, or on the day of, the permission. Contact HR to record a past one."
        )
    off = _day_kind_label(db, employee, day)
    if off:
        raise WorkflowError(f"{off}; no permission is needed.")
    minutes = _prepare_permission(db, employee, day, from_time, to_time)
    errors = permission_limit_errors(db, employee, day, minutes)
    if errors:
        raise WorkflowError(" ".join(errors))
    initial, manager = submit_routing(db, employee, REQUEST_PERMISSION)
    request = PermissionRequest(
        employee_id=employee.id, date=day, from_time=from_time, to_time=to_time, minutes=minutes,
        reason=(reason or "").strip() or None, status=initial,
        override_limits=False, applied_by_user_id=user.id, recorded_by_hr=False,
        manager_id=manager.id if manager else None,
    )
    db.add(request)
    db.flush()
    notify.submitted(db, request, user)
    return request


def record_permission_on_behalf(
    db: Session, *, hr_user: AdminUser, employee_id: int, day: date, from_time: time, to_time: time,
    reason: Optional[str], comment: Optional[str] = None, override_limits: bool = False,
    today: Optional[date] = None,
) -> tuple[PermissionRequest, list[str]]:
    today = today or date.today()
    employee = _get_employee(db, employee_id)
    _not_self(hr_user, employee.id, "record")
    minutes = _prepare_permission(db, employee, day, from_time, to_time)
    errors = permission_limit_errors(db, employee, day, minutes)
    if errors and not override_limits:
        raise WorkflowError(" ".join(errors) + " Tick 'Override limits' to record it anyway.")
    warnings = list(errors)
    off = _day_kind_label(db, employee, day)
    if off:
        warnings.append(f"{off}.")
    if day < today:
        warnings.append("Backdated permission recorded by HR.")
    request = PermissionRequest(
        employee_id=employee.id, date=day, from_time=from_time, to_time=to_time, minutes=minutes,
        reason=(reason or "").strip() or None, status=STATUS_APPROVED, override_limits=bool(errors),
        applied_by_user_id=hr_user.id, recorded_by_hr=True, manager_id=_manager_id(employee),
        hr_action_by=hr_user.id, hr_action_at=_now(), hr_comment=(comment or "").strip() or None,
    )
    db.add(request)
    db.flush()
    notify.recorded(db, request, hr_user)
    return request, warnings


# --- re-routing when a manager becomes eligible -----------------------------

REROUTE_MODELS = (LeaveApplication, PermissionRequest, WfhRequest, AttendanceRegularization)
MODEL_FOR_TYPE = {
    REQUEST_LEAVE: LeaveApplication,
    REQUEST_PERMISSION: PermissionRequest,
    REQUEST_WFH: WfhRequest,
    REQUEST_REGULARIZATION: AttendanceRegularization,
}


def _skipped_manager(query, model):
    return query.filter(model.status == STATUS_PENDING_HR, model.manager_id.is_(None),
                        model.recorded_by_hr.is_(False), model.manager_action_by.is_(None))


def _reroute_rows(db: Session, rows) -> int:
    moved = 0
    for request in rows:
        manager = approving_manager(db, request.employee, request_type_of(request))
        if manager is None:
            continue
        request.status = STATUS_PENDING_MANAGER
        request.manager_id = manager.id
        db.flush()
        notify.rerouted(db, request)
        moved += 1
    return moved


def reroute_pending(db: Session, department_id: int) -> int:
    """Moves PENDING_HR requests of the department's employees to
    PENDING_MANAGER when they skipped the manager step (manager_id NULL,
    not recorded by HR, never acted on by a manager), their chain includes
    a manager and an eligible manager now exists. The manager is notified.
    Returns how many were moved."""
    db.flush()
    department = db.get(Department, department_id)
    if department is not None:
        db.expire(department, ["manager"])
    moved = 0
    for model in REROUTE_MODELS:
        query = db.query(model).join(Employee, Employee.id == model.employee_id).filter(
            Employee.department_id == department_id)
        moved += _reroute_rows(db, _skipped_manager(query, model).order_by(model.id).all())
    return moved


def reroute_request_type(db: Session, request_type: str) -> int:
    """reroute_pending for every employee, for one request type: used when
    its approval chain changes to one that includes a manager."""
    db.flush()
    model = MODEL_FOR_TYPE[request_type]
    return _reroute_rows(db, _skipped_manager(db.query(model), model).order_by(model.id).all())


def reroute_for_manager(db: Session, employee_id: int) -> int:
    """reroute_pending for every department the employee manages and every
    department of their direct reports (the reporting-manager fallback)."""
    ids = {row[0] for row in db.query(Department.id).filter(Department.manager_employee_id == employee_id)}
    ids |= {
        row[0] for row in db.query(Employee.department_id).filter(
            Employee.reporting_manager_id == employee_id, Employee.department_id.isnot(None))
    }
    return sum(reroute_pending(db, department_id) for department_id in sorted(ids))
