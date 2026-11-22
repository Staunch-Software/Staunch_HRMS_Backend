"""Work-from-home requests. Same workflow as leave (the WFH approval chain,
by default department manager then HR; HR can record on behalf, approve or
reject once the manager has approved, and cancel):
the approval steps and cancellation are the generic ones in workflow.py.

Days are the working days in the range per the attendance calendar (no
sandwich rule); a single-day request may be a half day.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import (
    ACTIVE_STATUSES,
    HALF_FULL,
    HALVES,
    STATUS_APPROVED,
    LeaveApplication,
)
from app.models.user import AdminUser
from app.models.wfh import WfhRequest
from app.services.leave import notify
from app.services.leave.calendar import KIND_WORK, day_kinds, policy_for
from app.services.leave.days import half_slots
from app.services.leave.workflow import (
    WorkflowError,
    _get_employee,
    _is_active,
    _manager_id,
    _not_self,
    _now,
    fmt_date,
    submit_routing,
)
from app.services.approval_settings import REQUEST_WFH

ZERO, HALF, ONE = Decimal("0"), Decimal("0.5"), Decimal("1")
MAX_RANGE_DAYS = 366


@dataclass
class WfhDay:
    date: date
    kind: str
    holiday_name: Optional[str]
    counted: Decimal
    half: str


def validate_half(from_date: date, to_date: date, half: Optional[str]) -> str:
    half = (half or HALF_FULL).upper()
    if half not in HALVES:
        raise WorkflowError("Half must be FULL, FIRST_HALF or SECOND_HALF.")
    if to_date < from_date:
        raise WorkflowError("The end date must be on or after the start date.")
    if half != HALF_FULL and to_date != from_date:
        raise WorkflowError("A half-day WFH request must be for a single day.")
    if (to_date - from_date).days > MAX_RANGE_DAYS:
        raise WorkflowError("The date range is too long.")
    return half


def wfh_breakdown(db: Session, employee: Employee, from_date: date, to_date: date, half: str) -> list[WfhDay]:
    fraction = ONE if half == HALF_FULL else HALF
    return [
        WfhDay(d, kind, holiday.name if holiday else None, fraction if kind == KIND_WORK else ZERO, half)
        for d, (kind, holiday) in day_kinds(db, employee, from_date, to_date).items()
    ]


def _slots(request) -> set:
    return half_slots(request.from_date, request.half, request.to_date, request.half)


def _working_slots(breakdown: list[WfhDay]) -> set:
    slots = set()
    for d in breakdown:
        if d.counted > 0:
            slots |= half_slots(d.date, d.half, d.date, d.half)
    return slots


def _check_overlaps(db: Session, employee_id: int, from_date: date, to_date: date, wanted: set,
                    exclude_id: Optional[int] = None) -> None:
    others = db.query(WfhRequest).filter(
        WfhRequest.employee_id == employee_id, WfhRequest.status.in_(ACTIVE_STATUSES),
        WfhRequest.from_date <= to_date, WfhRequest.to_date >= from_date,
    )
    for other in others:
        if other.id != exclude_id and wanted & _slots(other):
            raise WorkflowError(
                f"This overlaps an existing WFH request ({fmt_date(other.from_date)} to {fmt_date(other.to_date)}, "
                f"{other.status.replace('_', ' ').lower()})."
            )
    leaves = db.query(LeaveApplication).filter(
        LeaveApplication.employee_id == employee_id, LeaveApplication.status.in_(ACTIVE_STATUSES),
        LeaveApplication.from_date <= to_date, LeaveApplication.to_date >= from_date,
    )
    for leave in leaves:
        if wanted & half_slots(leave.from_date, leave.from_half, leave.to_date, leave.to_half):
            raise WorkflowError(
                f"There is already a leave request on these dates ({fmt_date(leave.from_date)} to "
                f"{fmt_date(leave.to_date)}, {leave.status.replace('_', ' ').lower()})."
            )


def _prepare(db: Session, employee: Employee, from_date: date, to_date: date, half: Optional[str]):
    half = validate_half(from_date, to_date, half)
    breakdown = wfh_breakdown(db, employee, from_date, to_date, half)
    days = sum((d.counted for d in breakdown), ZERO)
    if days <= 0:
        raise WorkflowError("The selected dates have no working days, so no WFH request is needed.")
    _check_overlaps(db, employee.id, from_date, to_date, _working_slots(breakdown))
    return half, days


def apply_wfh(
    db: Session, *, employee: Employee, user: AdminUser, from_date: date, to_date: date, half: Optional[str],
    reason: Optional[str], today: Optional[date] = None,
) -> WfhRequest:
    today = today or date.today()
    if not _is_active(employee):
        raise WorkflowError("Only active employees can request work from home.", 403)
    if from_date < today:
        raise WorkflowError("Backdated WFH cannot be requested. Contact HR to record it.")
    notice_days = policy_for(db, employee).wfh_min_notice_days or 0
    earliest = today + timedelta(days=notice_days)
    if from_date < earliest:
        raise WorkflowError(
            f"WFH must be requested at least {notice_days} day(s) in advance (earliest start date "
            f"{fmt_date(earliest)}). For an emergency, contact HR."
        )
    half, days = _prepare(db, employee, from_date, to_date, half)
    initial, manager = submit_routing(db, employee, REQUEST_WFH)
    request = WfhRequest(
        employee_id=employee.id, from_date=from_date, to_date=to_date, half=half, days=days,
        reason=(reason or "").strip() or None, status=initial,
        applied_by_user_id=user.id, recorded_by_hr=False, manager_id=manager.id if manager else None,
    )
    db.add(request)
    db.flush()
    notify.submitted(db, request, user)
    return request


def record_wfh_on_behalf(
    db: Session, *, hr_user: AdminUser, employee_id: int, from_date: date, to_date: date, half: Optional[str],
    reason: Optional[str], comment: Optional[str] = None, today: Optional[date] = None,
) -> tuple[WfhRequest, list[str]]:
    """HR records WFH that is approved immediately; backdating and the
    minimum notice produce warnings, not errors."""
    today = today or date.today()
    employee = _get_employee(db, employee_id)
    _not_self(hr_user, employee.id, "record")
    half, days = _prepare(db, employee, from_date, to_date, half)
    warnings = []
    if not _is_active(employee):
        warnings.append(f"The employee's status is {employee.status}.")
    if from_date < today:
        warnings.append("Backdated WFH recorded by HR.")
    else:
        notice_days = policy_for(db, employee).wfh_min_notice_days or 0
        if from_date < today + timedelta(days=notice_days):
            warnings.append(f"Recorded with less than the {notice_days}-day minimum notice.")
    request = WfhRequest(
        employee_id=employee.id, from_date=from_date, to_date=to_date, half=half, days=days,
        reason=(reason or "").strip() or None, status=STATUS_APPROVED, applied_by_user_id=hr_user.id,
        recorded_by_hr=True, manager_id=_manager_id(employee), hr_action_by=hr_user.id, hr_action_at=_now(),
        hr_comment=(comment or "").strip() or None,
    )
    db.add(request)
    db.flush()
    notify.recorded(db, request, hr_user)
    return request, warnings


def _approved(db: Session, date_from: date, date_to: date, employee_id: Optional[int] = None):
    query = db.query(WfhRequest).filter(
        WfhRequest.status == STATUS_APPROVED, WfhRequest.from_date <= date_to, WfhRequest.to_date >= date_from,
    )
    if employee_id is not None:
        query = query.filter(WfhRequest.employee_id == employee_id)
    return query.order_by(WfhRequest.from_date, WfhRequest.id).all()


def approved_wfh_by_day(db: Session, employee_id: int, date_from: date, date_to: date) -> dict[date, str]:
    """{date: FULL | FIRST_HALF | SECOND_HALF} of APPROVED WFH (every date in
    the range; attendance processing ignores off days)."""
    result: dict[date, str] = {}
    for request in _approved(db, date_from, date_to, employee_id):
        day = max(request.from_date, date_from)
        while day <= min(request.to_date, date_to):
            previous = result.get(day)
            result[day] = HALF_FULL if previous and previous != request.half else request.half
            day += timedelta(days=1)
    return result


def approved_wfh_ids(db: Session, date_from: date, date_to: date) -> dict[date, set[int]]:
    """{date: employee ids with approved WFH (full or half) that day}."""
    result: dict[date, set[int]] = defaultdict(set)
    for request in _approved(db, date_from, date_to):
        day = max(request.from_date, date_from)
        while day <= min(request.to_date, date_to):
            result[day].add(request.employee_id)
            day += timedelta(days=1)
    return result
