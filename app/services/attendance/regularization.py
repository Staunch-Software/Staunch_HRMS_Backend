"""Missed-punch requests (AttendanceRegularization).

An employee asks for a missing IN and/or OUT time on a past day (not in the
future, at most regularization_max_back_days back, at most
regularization_monthly_cap pending or approved requests per month, one open
request per day). Routing and the approval steps are the generic ones in
services/leave/workflow.py under the REGULARIZATION chain. On final approval
an AttendanceOverride (source REGULARIZATION) fills only the side that is
still missing; a side the device has delivered meanwhile is left alone. A
month already processed for the employee is processed again for them.
"""
from __future__ import annotations

import logging
from datetime import date, time, timedelta
from typing import Optional

from sqlalchemy import extract
from sqlalchemy.orm import Session

from app.models.attendance_month import AttendanceMonthResult
from app.models.attendance_policy import POLICY_DEFAULTS
from app.models.employee import Employee
from app.models.leave import ACTIVE_STATUSES, PENDING_STATUSES, STATUS_APPROVED
from app.models.regularization import REASON_CODES, REASON_LABELS, REASON_OTHER, AttendanceRegularization
from app.models.user import AdminUser
from app.services.approval_settings import REQUEST_REGULARIZATION
from app.services.attendance.effective import EffectiveDay, effective_days
from app.services.attendance.overrides import (
    KIND_STATUS,
    KIND_TIMES,
    OverrideError,
    build_values,
    get_active,
    revert_override,
    save_override,
)
from app.services.leave import notify
from app.services.leave.calendar import policy_for
from app.services.leave.workflow import WorkflowError, _is_active, fmt_date, register_approval_hooks, submit_routing

logger = logging.getLogger(__name__)

SOURCE_REGULARIZATION = "REGULARIZATION"


def _policy_value(policy, name: str) -> int:
    value = getattr(policy, name, None)
    return int(POLICY_DEFAULTS[name] if value is None else value)


def max_back_days(policy) -> int:
    return _policy_value(policy, "regularization_max_back_days")


def monthly_cap(policy) -> int:
    return _policy_value(policy, "regularization_monthly_cap")


def month_usage(db: Session, employee_id: int, day: date, exclude_id: Optional[int] = None) -> int:
    """Pending + approved requests for days in day's month."""
    rows = db.query(AttendanceRegularization.id).filter(
        AttendanceRegularization.employee_id == employee_id,
        AttendanceRegularization.status.in_(ACTIVE_STATUSES),
        extract("year", AttendanceRegularization.date) == day.year,
        extract("month", AttendanceRegularization.date) == day.month,
    )
    return sum(1 for (rid,) in rows if rid != exclude_id)


def effective_day(db: Session, employee_id: int, day: date) -> Optional[EffectiveDay]:
    return effective_days(db, [employee_id], day, day).get((employee_id, day))


def _hhmm(value) -> str:
    return value.strftime("%H:%M")


def _check_sides(eff: Optional[EffectiveDay], punch_in: Optional[time], punch_out: Optional[time],
                 day: date) -> None:
    """Rejects a request for a side the day already has, a day HR has set a
    status on, and times that would end before they start."""
    if eff is not None and eff.kind == KIND_STATUS:
        raise WorkflowError(f"HR has already set the attendance for {fmt_date(day)} ({eff.status_code}); "
                            "contact HR to change it.")
    have_in = eff.punch_in if eff is not None else None
    have_out = eff.punch_out if eff is not None else None
    if punch_in is not None and have_in is not None:
        raise WorkflowError(f"An IN punch is already recorded for {fmt_date(day)} ({_hhmm(have_in)}).")
    if punch_out is not None and have_out is not None:
        raise WorkflowError(f"An OUT punch is already recorded for {fmt_date(day)} ({_hhmm(have_out)}).")
    start = punch_in if punch_in is not None else (have_in.time() if have_in is not None else None)
    end = punch_out if punch_out is not None else (have_out.time() if have_out is not None else None)
    if start is not None and end is not None and end <= start:
        raise WorkflowError(f"The OUT time must be after the IN time ({_hhmm(start)}).")


def validate(
    db: Session, employee: Employee, day: date, punch_in: Optional[time], punch_out: Optional[time],
    reason_code: str, reason: Optional[str], today: date,
) -> str:
    """Raises WorkflowError for a request the employee may not submit.
    Returns the normalised reason code."""
    reason_code = (reason_code or "").strip().upper()
    if reason_code not in REASON_CODES:
        raise WorkflowError(f"Reason must be one of: {', '.join(REASON_LABELS.values())}.")
    if reason_code == REASON_OTHER and not (reason or "").strip():
        raise WorkflowError("Describe the reason when choosing Other.")
    if punch_in is None and punch_out is None:
        raise WorkflowError("Give the missing IN time, OUT time or both.")
    if day > today:
        raise WorkflowError("A missed punch cannot be requested for a future date.")
    policy = policy_for(db, employee)
    back = max_back_days(policy)
    if day < today - timedelta(days=back):
        raise WorkflowError(f"Missed punches can be requested for the last {back} day(s) only "
                            f"(earliest {fmt_date(today - timedelta(days=back))}). Contact HR for older days.")
    open_request = db.query(AttendanceRegularization).filter(
        AttendanceRegularization.employee_id == employee.id,
        AttendanceRegularization.date == day,
        AttendanceRegularization.status.in_(PENDING_STATUSES),
    ).first()
    if open_request is not None:
        raise WorkflowError(f"There is already an open missed-punch request for {fmt_date(day)}.")
    cap = monthly_cap(policy)
    used = month_usage(db, employee.id, day)
    if used >= cap:
        raise WorkflowError(f"Only {cap} missed-punch request(s) are allowed per month "
                            f"({used} already for {day.strftime('%b %Y')}). Contact HR.")
    _check_sides(effective_day(db, employee.id, day), punch_in, punch_out, day)
    return reason_code


def apply_regularization(
    db: Session, *, employee: Employee, user: AdminUser, day: date, punch_in: Optional[time],
    punch_out: Optional[time], reason_code: str, reason: Optional[str], today: Optional[date] = None,
) -> AttendanceRegularization:
    today = today or date.today()
    if not _is_active(employee):
        raise WorkflowError("Only active employees can request a missed punch.", 403)
    reason_code = validate(db, employee, day, punch_in, punch_out, reason_code, reason, today)
    initial, manager = submit_routing(db, employee, REQUEST_REGULARIZATION)
    request = AttendanceRegularization(
        employee_id=employee.id, date=day, punch_in=punch_in, punch_out=punch_out, reason_code=reason_code,
        reason=(reason or "").strip() or None, status=initial, applied_by_user_id=user.id, recorded_by_hr=False,
        manager_id=manager.id if manager else None,
    )
    db.add(request)
    db.flush()
    notify.submitted(db, request, user)
    return request


def rules(db: Session, employee: Employee, today: Optional[date] = None) -> dict:
    from app.services.approval_settings import chain_for
    from app.services.leave.workflow import approving_manager
    from app.services.reporting import employee_full_name

    today = today or date.today()
    policy = policy_for(db, employee)
    manager = approving_manager(db, employee, REQUEST_REGULARIZATION)
    return {
        "max_back_days": max_back_days(policy),
        "monthly_cap": monthly_cap(policy),
        "used_this_month": month_usage(db, employee.id, today),
        "earliest_date": today - timedelta(days=max_back_days(policy)),
        "reasons": [{"code": code, "label": label} for code, label in REASON_LABELS.items()],
        "has_manager": manager is not None,
        "manager_name": employee_full_name(manager),
        "chain": chain_for(db, REQUEST_REGULARIZATION),
    }


# --- final approval hooks ----------------------------------------------------------

def _missing_sides(db: Session, request: AttendanceRegularization) -> tuple[Optional[time], Optional[time]]:
    """The requested times for the sides the day still lacks."""
    eff = effective_day(db, request.employee_id, request.date)
    if eff is not None and eff.kind == KIND_STATUS:
        raise WorkflowError(f"HR has set the attendance for {fmt_date(request.date)} ({eff.status_code}) since "
                            "this request was made; change it in the Attendance Log instead.")
    have_in = eff.punch_in if eff is not None else None
    have_out = eff.punch_out if eff is not None else None
    fill_in = request.punch_in if have_in is None else None
    fill_out = request.punch_out if have_out is None else None
    start = fill_in or (have_in.time() if have_in is not None else None)
    end = fill_out or (have_out.time() if have_out is not None else None)
    if (fill_in or fill_out) and start is not None and end is not None and end <= start:
        raise WorkflowError(f"With the punches now recorded for {fmt_date(request.date)}, the OUT time "
                            f"({_hhmm(end)}) would not be after the IN time ({_hhmm(start)}).")
    return fill_in, fill_out


def _check(db: Session, request: AttendanceRegularization) -> None:
    _missing_sides(db, request)


def _reprocess(db: Session, request: AttendanceRegularization, user: AdminUser) -> None:
    from app.services.attendance import monthly

    month = request.date.strftime("%Y-%m")
    row = db.query(AttendanceMonthResult).filter(
        AttendanceMonthResult.employee_id == request.employee_id, AttendanceMonthResult.month == month,
    ).first()
    if row is None or row.pay_group_id is None:
        return
    try:
        with db.begin_nested():
            monthly.process_month(db, month, row.pay_group_id, user, employee_ids=[request.employee_id])
    except Exception:  # noqa: BLE001 - HR can still process the month by hand
        logger.exception("Re-processing %s for employee %s after missed punch %s failed",
                         month, request.employee_id, request.id)


def _apply(db: Session, request: AttendanceRegularization, user: AdminUser) -> None:
    fill_in, fill_out = _missing_sides(db, request)
    sides = [side for side, value in (("IN", fill_in), ("OUT", fill_out)) if value is not None]
    request.applied_sides = ",".join(sides)
    if not sides:
        db.flush()
        return
    current = get_active(db, request.employee_id, request.date)
    keep_in = current.punch_in.time() if current is not None and current.kind == KIND_TIMES and current.punch_in else None
    keep_out = (current.punch_out.time()
                if current is not None and current.kind == KIND_TIMES and current.punch_out else None)
    label = REASON_LABELS.get(request.reason_code, request.reason_code)
    note = f"Missed punch request #{request.id}: {label}" + (f" - {request.reason}" if request.reason else "")
    try:
        values = build_values(kind=KIND_TIMES, work_date=request.date, punch_in=fill_in or keep_in,
                              punch_out=fill_out or keep_out, reason=note[:1000])
    except OverrideError as exc:
        raise WorkflowError(f"The missed punch cannot be applied: {exc}.") from exc
    employee = request.employee or db.get(Employee, request.employee_id)
    row, _ = save_override(db, request.employee_id, employee.employee_code, request.date, values,
                           source=SOURCE_REGULARIZATION, created_by=user.username)
    db.flush()
    request.override_id = row.id
    db.flush()
    _reprocess(db, request, user)


register_approval_hooks(REQUEST_REGULARIZATION, check=_check, apply=_apply)


def describe_times(request: AttendanceRegularization) -> str:
    parts = []
    if request.punch_in is not None:
        parts.append(f"IN {_hhmm(request.punch_in)}")
    if request.punch_out is not None:
        parts.append(f"OUT {_hhmm(request.punch_out)}")
    return ", ".join(parts)


def cancel_by_hr(db: Session, request: AttendanceRegularization, user: AdminUser,
                 comment: Optional[str] = None) -> AttendanceRegularization:
    """Cancels a pending or approved request. An approved one also removes
    the correction it wrote, when that correction is still the active one."""
    from app.services.leave.workflow import cancel_by_hr as workflow_cancel

    was_approved = request.status == STATUS_APPROVED
    workflow_cancel(db, request, user, comment)
    if was_approved and request.override_id is not None:
        current = get_active(db, request.employee_id, request.date)
        if current is not None and current.id == request.override_id:
            revert_override(db, request.employee_id, request.date, user.username)
            db.flush()
            _reprocess(db, request, user)
    return request
