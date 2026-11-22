"""Employee self-service work-from-home requests and the department
manager's WFH approval actions."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.v1.me import get_current_employee
from app.api.v1.me_leave import _manager_action, _raise
from app.core.db import get_db
from app.core.security import require_employee_user
from app.models.employee import Employee
from app.models.user import AdminUser
from app.models.wfh import WfhRequest
from app.schemas.leave import ActionComment
from app.schemas.wfh import WfhApplyRequest, WfhPreview, WfhRequestRead, WfhRules
from app.services.approval_settings import chain_for
from app.services.leave import wfh as wfh_service
from app.services.leave import workflow
from app.services.leave.calendar import policy_for
from app.services.leave.serialize import wfh_read, wfh_reads
from app.services.reporting import employee_full_name

router = APIRouter(prefix="/me", tags=["self-service"], dependencies=[Depends(require_employee_user)])


def wfh_preview(db: Session, employee: Employee, from_date: date, to_date: date, half: str) -> WfhPreview:
    try:
        half = wfh_service.validate_half(from_date, to_date, half)
    except workflow.WorkflowError as exc:
        return WfhPreview(days=0, breakdown=[], errors=[exc.message])
    breakdown = wfh_service.wfh_breakdown(db, employee, from_date, to_date, half)
    days = sum((d.counted for d in breakdown), wfh_service.ZERO)
    return WfhPreview(
        days=days,
        breakdown=[{"date": d.date, "kind": d.kind, "holiday_name": d.holiday_name, "counted": d.counted,
                    "half": d.half} for d in breakdown],
        errors=[] if days > 0 else ["The selected dates have no working days."],
    )


@router.get("/wfh-rules", response_model=WfhRules)
def my_wfh_rules(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    manager = workflow.approving_manager(db, employee, "WFH")
    return WfhRules(min_notice_days=policy_for(db, employee).wfh_min_notice_days, has_manager=manager is not None,
                    manager_name=employee_full_name(manager), chain=chain_for(db, "WFH"))


@router.get("/wfh-preview", response_model=WfhPreview)
def my_wfh_preview(
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
    half: str = "FULL",
    employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    return wfh_preview(db, employee, from_date, to_date, half)


@router.get("/wfh-requests", response_model=list[WfhRequestRead])
def my_wfh_requests(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    rows = (
        db.query(WfhRequest).filter(WfhRequest.employee_id == employee.id)
        .order_by(WfhRequest.from_date.desc(), WfhRequest.id.desc()).all()
    )
    return wfh_reads(db, rows)


@router.post("/wfh-requests", response_model=WfhRequestRead, status_code=status.HTTP_201_CREATED)
def apply_my_wfh(
    payload: WfhApplyRequest,
    employee: Employee = Depends(get_current_employee),
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    try:
        request = wfh_service.apply_wfh(
            db, employee=employee, user=user, from_date=payload.from_date, to_date=payload.to_date,
            half=payload.half, reason=payload.reason,
        )
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return wfh_read(db, request)


@router.post("/wfh-requests/{request_id}/cancel", response_model=WfhRequestRead)
def cancel_my_wfh(
    request_id: int,
    payload: Optional[ActionComment] = None,
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    request = db.get(WfhRequest, request_id)
    if request is None or request.employee_id != user.employee_id:
        raise HTTPException(status_code=404, detail="WFH request not found.")
    try:
        workflow.cancel_by_employee(db, request, user, payload.comment if payload else None)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return wfh_read(db, request)


@router.post("/approvals/wfh/{request_id}/approve", response_model=WfhRequestRead)
def manager_approve_wfh(request_id: int, payload: Optional[ActionComment] = None,
                        user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    return wfh_read(db, _manager_action(db, WfhRequest, request_id, user, True, payload))


@router.post("/approvals/wfh/{request_id}/reject", response_model=WfhRequestRead)
def manager_reject_wfh(request_id: int, payload: Optional[ActionComment] = None,
                       user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    return wfh_read(db, _manager_action(db, WfhRequest, request_id, user, False, payload))
