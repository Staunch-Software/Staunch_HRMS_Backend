"""Employee self-service missed-punch requests and the manager's approval
actions on them."""
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.v1.me import get_current_employee
from app.api.v1.me_leave import _manager_action, _raise
from app.core.db import get_db
from app.core.security import require_employee_user
from app.models.employee import Employee
from app.models.regularization import AttendanceRegularization
from app.models.user import AdminUser
from app.schemas.leave import ActionComment
from app.schemas.regularization import RegularizationApplyRequest, RegularizationRead, RegularizationRules
from app.services.attendance import regularization as service
from app.services.leave import workflow
from app.services.leave.serialize import regularization_read, regularization_reads

router = APIRouter(prefix="/me", tags=["self-service"], dependencies=[Depends(require_employee_user)])


class DayPunches(BaseModel):
    date: date
    punch_in: Optional[datetime] = None
    punch_out: Optional[datetime] = None
    status_code: Optional[str] = None


@router.get("/regularization-rules", response_model=RegularizationRules)
def my_regularization_rules(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    return service.rules(db, employee)


@router.get("/regularization-day", response_model=DayPunches)
def my_day_punches(day: date = Query(alias="date"), employee: Employee = Depends(get_current_employee),
                   db: Session = Depends(get_db)):
    """The IN / OUT currently recorded for the day (device data plus HR corrections)."""
    eff = service.effective_day(db, employee.id, day)
    if eff is None:
        return DayPunches(date=day)
    return DayPunches(date=day, punch_in=eff.punch_in, punch_out=eff.punch_out, status_code=eff.status_code)


@router.get("/regularizations", response_model=list[RegularizationRead])
def my_regularizations(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    rows = (
        db.query(AttendanceRegularization).filter(AttendanceRegularization.employee_id == employee.id)
        .order_by(AttendanceRegularization.date.desc(), AttendanceRegularization.id.desc()).all()
    )
    return regularization_reads(db, rows)


@router.post("/regularizations", response_model=RegularizationRead, status_code=status.HTTP_201_CREATED)
def apply_my_regularization(
    payload: RegularizationApplyRequest,
    employee: Employee = Depends(get_current_employee),
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    try:
        request = service.apply_regularization(
            db, employee=employee, user=user, day=payload.date, punch_in=payload.punch_in,
            punch_out=payload.punch_out, reason_code=payload.reason_code, reason=payload.reason,
        )
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return regularization_read(db, request)


@router.post("/regularizations/{request_id}/cancel", response_model=RegularizationRead)
def cancel_my_regularization(
    request_id: int,
    payload: Optional[ActionComment] = None,
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    request = db.get(AttendanceRegularization, request_id)
    if request is None or request.employee_id != user.employee_id:
        raise HTTPException(status_code=404, detail="Missed-punch request not found.")
    try:
        workflow.cancel_by_employee(db, request, user, payload.comment if payload else None)
    except workflow.WorkflowError as exc:
        db.rollback()
        _raise(exc)
    db.commit()
    db.refresh(request)
    return regularization_read(db, request)


@router.post("/approvals/regularization/{request_id}/approve", response_model=RegularizationRead)
def manager_approve_regularization(request_id: int, payload: Optional[ActionComment] = None,
                                   user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    return regularization_read(db, _manager_action(db, AttendanceRegularization, request_id, user, True, payload))


@router.post("/approvals/regularization/{request_id}/reject", response_model=RegularizationRead)
def manager_reject_regularization(request_id: int, payload: Optional[ActionComment] = None,
                                  user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)):
    return regularization_read(db, _manager_action(db, AttendanceRegularization, request_id, user, False, payload))
