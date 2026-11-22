"""HR missed-punch administration: the request queue, approve and reject.
Approval writes the attendance correction (services/attendance/regularization)."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.v1.leave_admin import _raise, _statuses
from app.core.db import get_db
from app.core.security import require_hr
from app.models.regularization import AttendanceRegularization
from app.models.user import AdminUser
from app.schemas.leave import ActionComment
from app.schemas.regularization import RegularizationRead
from app.services.attendance import regularization
from app.services.leave import workflow
from app.services.leave.serialize import regularization_read, regularization_reads

router = APIRouter(tags=["regularizations"])


@router.get("/regularizations", response_model=list[RegularizationRead])
def list_regularizations(
    status_filter: Optional[str] = Query(default=None, alias="status"),
    employee_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(default=500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    query = db.query(AttendanceRegularization)
    statuses = _statuses(status_filter)
    if statuses:
        query = query.filter(AttendanceRegularization.status.in_(statuses))
    if employee_id is not None:
        query = query.filter(AttendanceRegularization.employee_id == employee_id)
    if date_from is not None:
        query = query.filter(AttendanceRegularization.date >= date_from)
    if date_to is not None:
        query = query.filter(AttendanceRegularization.date <= date_to)
    rows = query.order_by(AttendanceRegularization.date.desc(), AttendanceRegularization.id.desc()).limit(limit).all()
    return regularization_reads(db, rows)


def _get(db: Session, request_id: int) -> AttendanceRegularization:
    request = db.get(AttendanceRegularization, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail="Missed-punch request not found.")
    return request


@router.get("/regularizations/{request_id}", response_model=RegularizationRead)
def get_regularization(request_id: int, db: Session = Depends(get_db)):
    return regularization_read(db, _get(db, request_id))


def _action(db: Session, request_id: int, action) -> RegularizationRead:
    request = _get(db, request_id)
    try:
        action(request)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return regularization_read(db, request)


@router.post("/regularizations/{request_id}/approve", response_model=RegularizationRead)
def approve_regularization(request_id: int, payload: Optional[ActionComment] = None,
                           db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _action(db, request_id, lambda r: workflow.hr_approve(db, r, user, comment))


@router.post("/regularizations/{request_id}/reject", response_model=RegularizationRead)
def reject_regularization(request_id: int, payload: Optional[ActionComment] = None,
                          db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    comment = payload.comment if payload else None
    return _action(db, request_id, lambda r: workflow.hr_reject(db, r, user, comment))


@router.post("/regularizations/{request_id}/cancel", response_model=RegularizationRead)
def cancel_regularization(request_id: int, payload: Optional[ActionComment] = None,
                          db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)):
    """Cancelling an approved request also removes the correction it wrote."""
    comment = payload.comment if payload else None
    return _action(db, request_id, lambda r: regularization.cancel_by_hr(db, r, user, comment))
