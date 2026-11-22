"""HR work-from-home administration: the request queue, recording on behalf
of an employee (approved immediately, may be backdated), approve, reject and
cancel."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.v1.leave_admin import _raise, _statuses
from app.api.v1.me_wfh import wfh_preview
from app.core.db import get_db
from app.core.security import require_hr
from app.models.employee import Employee
from app.models.user import AdminUser
from app.models.wfh import WfhRequest
from app.schemas.leave import ActionComment
from app.schemas.wfh import WfhPreview, WfhRecordRequest, WfhRequestRead
from app.services.leave import wfh as wfh_service
from app.services.leave import workflow
from app.services.leave.serialize import wfh_read, wfh_reads

router = APIRouter(tags=["wfh"])


@router.get("/wfh-requests", response_model=list[WfhRequestRead])
def list_wfh_requests(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    employee_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    query = db.query(WfhRequest)
    statuses = _statuses(status_filter)
    if statuses:
        query = query.filter(WfhRequest.status.in_(statuses))
    if employee_id is not None:
        query = query.filter(WfhRequest.employee_id == employee_id)
    if date_from is not None:
        query = query.filter(WfhRequest.to_date >= date_from)
    if date_to is not None:
        query = query.filter(WfhRequest.from_date <= date_to)
    rows = query.order_by(WfhRequest.from_date.desc(), WfhRequest.id.desc()).limit(limit).all()
    return wfh_reads(db, rows)


@router.get("/wfh-requests/preview", response_model=WfhPreview)
def preview_wfh_days(employee_id: int, from_date: date, to_date: date, half: str = "FULL",
                     db: Session = Depends(get_db)):
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found.")
    return wfh_preview(db, employee, from_date, to_date, half)


def _get(db: Session, request_id: int) -> WfhRequest:
    request = db.get(WfhRequest, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="WFH request not found.")
    return request


@router.get("/wfh-requests/{request_id}", response_model=WfhRequestRead)
def get_wfh_request(request_id: int, db: Session = Depends(get_db)):
    return wfh_read(db, _get(db, request_id))


@router.post("/wfh-requests", response_model=WfhRequestRead, status_code=status.HTTP_201_CREATED)
def record_wfh(payload: WfhRecordRequest, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    try:
        request, warnings = wfh_service.record_wfh_on_behalf(
            db, hr_user=user, employee_id=payload.employee_id, from_date=payload.from_date,
            to_date=payload.to_date, half=payload.half, reason=payload.reason, comment=payload.comment,
        )
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return wfh_read(db, request, warnings=warnings)


def _action(db: Session, request_id: int, action) -> WfhRequestRead:
    request = _get(db, request_id)
    try:
        action(request)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return wfh_read(db, request)


@router.post("/wfh-requests/{request_id}/approve", response_model=WfhRequestRead)
def approve_wfh(request_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
                user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _action(db, request_id, lambda r: workflow.hr_approve(db, r, user, comment))


@router.post("/wfh-requests/{request_id}/reject", response_model=WfhRequestRead)
def reject_wfh(request_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
               user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _action(db, request_id, lambda r: workflow.hr_reject(db, r, user, comment))


@router.post("/wfh-requests/{request_id}/cancel", response_model=WfhRequestRead)
def cancel_wfh(request_id: int, payload: Optional[ActionComment] = None, db: Session = Depends(get_db),
               user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _action(db, request_id, lambda r: workflow.cancel_by_hr(db, r, user, comment))
