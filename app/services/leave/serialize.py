"""Builds API read models for leave, permission, WFH and missed-punch requests."""
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.leave import (
    STATUS_APPROVED,
    STATUS_CANCELLED,
    STATUS_PENDING_HR,
    STATUS_PENDING_MANAGER,
    STATUS_REJECTED,
    LeaveApplication,
    PermissionRequest,
)
from app.models.regularization import REASON_LABELS
from app.models.user import AdminUser
from app.schemas.leave import LeaveApplicationRead, PermissionRequestRead
from app.schemas.regularization import RegularizationRead
from app.schemas.wfh import WfhRequestRead
from app.services.reporting import employee_full_name

STEP_LABELS = {
    STATUS_PENDING_MANAGER: "Awaiting manager",
    STATUS_PENDING_HR: "Awaiting HR",
    STATUS_APPROVED: "Approved",
    STATUS_REJECTED: "Rejected",
    STATUS_CANCELLED: "Cancelled",
}


class UserLabels:
    """Caches 'Name (username)'-style labels for action-by user ids."""

    def __init__(self, db: Session):
        self.db = db
        self.cache: dict[int, Optional[str]] = {}

    def __call__(self, user_id: Optional[int]) -> Optional[str]:
        if user_id is None:
            return None
        if user_id not in self.cache:
            user = self.db.get(AdminUser, user_id)
            if user is None:
                self.cache[user_id] = None
            else:
                name = employee_full_name(user.employee) if user.employee_id else None
                self.cache[user_id] = name or user.username
        return self.cache[user_id]


def _step(request) -> str:
    if request.status == STATUS_REJECTED:
        stage = "manager" if request.manager_action_by and not request.hr_action_by else "HR"
        return f"Rejected by {stage}"
    if request.status == STATUS_APPROVED and request.recorded_by_hr:
        return "Recorded by HR"
    return STEP_LABELS.get(request.status, request.status)


def _common(request, labels: UserLabels, warnings: Optional[list[str]]) -> dict:
    employee = request.employee
    manager = request.manager
    return {
        "id": request.id,
        "employee_id": request.employee_id,
        "employee_code": employee.employee_code if employee else None,
        "employee_name": employee_full_name(employee),
        "status": request.status,
        "step": _step(request),
        "reason": request.reason,
        "recorded_by_hr": request.recorded_by_hr,
        "applied_by": labels(request.applied_by_user_id),
        "manager_id": request.manager_id,
        "manager_name": employee_full_name(manager),
        "manager_action_by": labels(request.manager_action_by),
        "manager_action_at": request.manager_action_at,
        "manager_comment": request.manager_comment,
        "hr_action_by": labels(request.hr_action_by),
        "hr_action_at": request.hr_action_at,
        "hr_comment": request.hr_comment,
        "cancelled_by": labels(request.cancelled_by),
        "cancelled_at": request.cancelled_at,
        "cancel_comment": request.cancel_comment,
        "created_at": request.created_at,
        "warnings": warnings or [],
    }


def leave_read(
    db: Session, application: LeaveApplication, labels: Optional[UserLabels] = None,
    warnings: Optional[list[str]] = None,
) -> LeaveApplicationRead:
    labels = labels or UserLabels(db)
    leave_type = application.leave_type
    return LeaveApplicationRead(
        **_common(application, labels, warnings),
        leave_type_id=application.leave_type_id,
        leave_type_code=leave_type.code if leave_type else None,
        leave_type_name=leave_type.name if leave_type else None,
        from_date=application.from_date,
        to_date=application.to_date,
        from_half=application.from_half,
        to_half=application.to_half,
        days=application.days,
    )


def permission_read(
    db: Session, request: PermissionRequest, labels: Optional[UserLabels] = None,
    warnings: Optional[list[str]] = None,
) -> PermissionRequestRead:
    labels = labels or UserLabels(db)
    return PermissionRequestRead(
        **_common(request, labels, warnings),
        date=request.date,
        from_time=request.from_time,
        to_time=request.to_time,
        minutes=request.minutes,
        override_limits=request.override_limits,
    )


def leave_reads(db: Session, applications: Iterable[LeaveApplication]) -> list[LeaveApplicationRead]:
    labels = UserLabels(db)
    return [leave_read(db, a, labels) for a in applications]


def permission_reads(db: Session, requests: Iterable[PermissionRequest]) -> list[PermissionRequestRead]:
    labels = UserLabels(db)
    return [permission_read(db, r, labels) for r in requests]


def wfh_read(db: Session, request, labels: Optional[UserLabels] = None,
             warnings: Optional[list[str]] = None) -> WfhRequestRead:
    labels = labels or UserLabels(db)
    return WfhRequestRead(
        **_common(request, labels, warnings),
        from_date=request.from_date,
        to_date=request.to_date,
        half=request.half,
        days=request.days,
    )


def wfh_reads(db: Session, requests: Iterable) -> list[WfhRequestRead]:
    labels = UserLabels(db)
    return [wfh_read(db, r, labels) for r in requests]


def regularization_read(db: Session, request, labels: Optional[UserLabels] = None,
                        warnings: Optional[list[str]] = None) -> RegularizationRead:
    labels = labels or UserLabels(db)
    return RegularizationRead(
        **_common(request, labels, warnings),
        date=request.date,
        punch_in=request.punch_in,
        punch_out=request.punch_out,
        reason_code=request.reason_code,
        reason_label=REASON_LABELS.get(request.reason_code, request.reason_code),
        applied_sides=request.applied_sides,
        override_id=request.override_id,
    )


def regularization_reads(db: Session, requests: Iterable) -> list[RegularizationRead]:
    labels = UserLabels(db)
    return [regularization_read(db, r, labels) for r in requests]
