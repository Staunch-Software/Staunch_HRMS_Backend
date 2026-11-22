"""Notifications for the leave / permission / WFH / missed-punch workflow. Every hook is guarded:
a failure is logged and never affects the workflow step that called it."""
from decimal import Decimal
from typing import Optional, Union

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import STATUS_PENDING_HR, STATUS_PENDING_MANAGER, LeaveApplication, PermissionRequest
from app.models.regularization import REASON_LABELS, AttendanceRegularization
from app.models.user import AdminUser
from app.models.wfh import WfhRequest
from app.services.notifications import guarded, hr_user_ids, notify_users, user_id_for_employee
from app.services.reporting import employee_full_name

Request = Union[LeaveApplication, PermissionRequest, WfhRequest, AttendanceRegularization]

LINK_MANAGER = "/portal/team-approvals"
LINK_EMPLOYEE = "/portal/requests"


def _is_leave(request: Request) -> bool:
    return isinstance(request, LeaveApplication)


def _is_wfh(request: Request) -> bool:
    return isinstance(request, WfhRequest)


def _is_regularization(request: Request) -> bool:
    return isinstance(request, AttendanceRegularization)


def _kind(request: Request) -> str:
    if _is_regularization(request):
        return "REGULARIZATION"
    return "LEAVE" if _is_leave(request) else ("WFH" if _is_wfh(request) else "PERMISSION")


def _noun(request: Request) -> str:
    if _is_regularization(request):
        return "missed punch"
    return "leave" if _is_leave(request) else ("work from home" if _is_wfh(request) else "permission")


def _hr_link(request: Request) -> str:
    if _is_regularization(request):
        return "/app/leave/missed-punches"
    if _is_wfh(request):
        return "/app/leave/wfh"
    return "/app/leave/requests" if _is_leave(request) else "/app/leave/permissions"


def _who(employee: Optional[Employee]) -> str:
    if employee is None:
        return "An employee"
    name = employee_full_name(employee) or employee.employee_code
    return f"{name} ({employee.employee_code})" if employee.employee_code and name != employee.employee_code else name


def _day(value) -> str:
    return value.strftime("%d %b %Y")


def _describe_wfh(request: WfhRequest) -> str:
    days = format(Decimal(request.days).normalize(), "f")
    span = _day(request.from_date)
    if request.to_date != request.from_date:
        span += f" to {_day(request.to_date)}"
    elif request.half != "FULL":
        span += " (first half)" if request.half == "FIRST_HALF" else " (second half)"
    return f"Work from home, {span} ({days} day{'' if Decimal(request.days) == 1 else 's'})"


def _describe_regularization(request: AttendanceRegularization) -> str:
    times = [f"{side} {value.strftime('%H:%M')}" for side, value in (("IN", request.punch_in),
                                                                     ("OUT", request.punch_out)) if value]
    reason = REASON_LABELS.get(request.reason_code, request.reason_code)
    return f"Missed punch on {_day(request.date)}: {', '.join(times)} ({reason})"


def describe(request: Request) -> str:
    if _is_regularization(request):
        return _describe_regularization(request)
    if _is_wfh(request):
        return _describe_wfh(request)
    if _is_leave(request):
        days = format(Decimal(request.days).normalize(), "f")
        span = _day(request.from_date)
        if request.to_date != request.from_date:
            span += f" to {_day(request.to_date)}"
        type_name = request.leave_type.name if request.leave_type else "Leave"
        return f"{type_name}, {span} ({days} day{'' if Decimal(request.days) == 1 else 's'})"
    return (f"Permission on {_day(request.date)}, {request.from_time.strftime('%H:%M')}-"
            f"{request.to_time.strftime('%H:%M')} ({request.minutes} min)")


def _with_comment(text: str, comment: Optional[str]) -> str:
    return f"{text} Comment: {comment}" if comment else text


def submitted(db: Session, request: Request, actor: AdminUser) -> None:
    kind = f"{_kind(request)}_SUBMITTED"
    with guarded(db, kind):
        title = f"{_noun(request).capitalize()} request from {_who(request.employee)}"
        what = describe(request)
        manager_user = user_id_for_employee(db, request.manager_id)
        if request.status == STATUS_PENDING_MANAGER and manager_user:
            notify_users(db, [manager_user], kind, title, f"{what}. Awaiting your approval.", LINK_MANAGER,
                         exclude=[actor.id])
        else:
            notify_users(db, hr_user_ids(db), kind, title, f"{what}. Awaiting HR approval.", _hr_link(request),
                         exclude=[actor.id])


def manager_decided(db: Session, request: Request, actor: AdminUser, approve: bool) -> None:
    with guarded(db, f"{_kind(request)}_MANAGER_DECISION"):
        who, what, noun = _who(request.employee), describe(request), _noun(request)
        employee_user = user_id_for_employee(db, request.employee_id)
        manager = _who(actor.employee) if actor.employee else actor.username
        if approve:
            notify_users(db, hr_user_ids(db), f"{_kind(request)}_PENDING_HR",
                         f"{noun.capitalize()} request awaiting HR approval",
                         _with_comment(f"{who}: {what}. Approved by manager {manager}.", request.manager_comment),
                         _hr_link(request), exclude=[actor.id])
            notify_users(db, [employee_user], f"{_kind(request)}_MANAGER_APPROVED",
                         f"Your {noun} request was approved by your manager",
                         _with_comment(f"{what}. It now awaits HR approval.", request.manager_comment),
                         LINK_EMPLOYEE, exclude=[actor.id])
        else:
            notify_users(db, [employee_user], f"{_kind(request)}_REJECTED",
                         f"Your {noun} request was rejected by your manager",
                         _with_comment(f"{what}.", request.manager_comment), LINK_EMPLOYEE, exclude=[actor.id])


def manager_final_approved(db: Session, request: Request, actor: AdminUser) -> None:
    """The manager's approval was final (MANAGER_ONLY chain)."""
    with guarded(db, f"{_kind(request)}_APPROVED"):
        notify_users(db, [user_id_for_employee(db, request.employee_id)], f"{_kind(request)}_APPROVED",
                     f"Your {_noun(request)} request was approved",
                     _with_comment(f"{describe(request)}. Approved by your manager.", request.manager_comment),
                     LINK_EMPLOYEE, exclude=[actor.id])


def rerouted(db: Session, request: Request) -> None:
    """A request that had gone to HR now awaits the manager (the manager
    became eligible, e.g. got a login)."""
    kind = f"{_kind(request)}_SUBMITTED"
    with guarded(db, kind):
        notify_users(db, [user_id_for_employee(db, request.manager_id)], kind,
                     f"{_noun(request).capitalize()} request from {_who(request.employee)}",
                     f"{describe(request)}. Awaiting your approval.", LINK_MANAGER)


def hr_decided(db: Session, request: Request, actor: AdminUser, approve: bool) -> None:
    event = "APPROVED" if approve else "REJECTED"
    with guarded(db, f"{_kind(request)}_{event}"):
        notify_users(db, [user_id_for_employee(db, request.employee_id)], f"{_kind(request)}_{event}",
                     f"Your {_noun(request)} request was {event.lower()}",
                     _with_comment(f"{describe(request)}.", request.hr_comment), LINK_EMPLOYEE, exclude=[actor.id])


def recorded(db: Session, request: Request, actor: AdminUser) -> None:
    with guarded(db, f"{_kind(request)}_RECORDED"):
        notify_users(db, [user_id_for_employee(db, request.employee_id)], f"{_kind(request)}_RECORDED",
                     f"HR recorded a {_noun(request)} for you",
                     _with_comment(f"{describe(request)}. Status: approved.", request.hr_comment),
                     LINK_EMPLOYEE, exclude=[actor.id])


def cancelled_by_employee(db: Session, request: Request, actor: AdminUser, previous_status: str) -> None:
    with guarded(db, f"{_kind(request)}_CANCELLED"):
        title = f"{_noun(request).capitalize()} request withdrawn by {_who(request.employee)}"
        message = _with_comment(f"{describe(request)}. No action is needed.", request.cancel_comment)
        manager_user = user_id_for_employee(db, request.manager_id)
        if previous_status == STATUS_PENDING_MANAGER and manager_user:
            notify_users(db, [manager_user], f"{_kind(request)}_CANCELLED", title, message, LINK_MANAGER,
                         exclude=[actor.id])
        elif previous_status in (STATUS_PENDING_MANAGER, STATUS_PENDING_HR):
            notify_users(db, hr_user_ids(db), f"{_kind(request)}_CANCELLED", title, message, _hr_link(request),
                         exclude=[actor.id])


def cancelled_by_hr(db: Session, request: Request, actor: AdminUser) -> None:
    with guarded(db, f"{_kind(request)}_CANCELLED"):
        notify_users(db, [user_id_for_employee(db, request.employee_id)], f"{_kind(request)}_CANCELLED",
                     f"Your {_noun(request)} was cancelled by HR",
                     _with_comment(f"{describe(request)}.", request.cancel_comment), LINK_EMPLOYEE,
                     exclude=[actor.id])
