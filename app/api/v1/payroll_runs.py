from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.schemas.payroll_run import (
    PayrollRunApprove,
    PayrollRunCreate,
    PayrollRunLineItemRead,
    PayrollRunRead,
)
from app.services.payroll_engine.engine import calculate_payroll_run

router = APIRouter(tags=["payroll-runs"], dependencies=[Depends(get_current_user)])


@router.post("/payroll-runs", response_model=PayrollRunRead, status_code=status.HTTP_201_CREATED)
def create_payroll_run(payload: PayrollRunCreate, db: Session = Depends(get_db)):
    if not db.get(PaySchedule, payload.pay_schedule_id):
        raise HTTPException(status_code=404, detail="Pay schedule not found")
    run = PayrollRun(status="Draft", source="InAppCalculation", **payload.model_dump())
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


@router.get("/payroll-runs", response_model=list[PayrollRunRead])
def list_payroll_runs(db: Session = Depends(get_db)):
    return db.query(PayrollRun).order_by(PayrollRun.period_start.desc(), PayrollRun.id.desc()).all()


@router.get("/payroll-runs/{run_id}", response_model=PayrollRunRead)
def get_payroll_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    return run


@router.get("/payroll-runs/{run_id}/line-items", response_model=list[PayrollRunLineItemRead])
def get_payroll_run_line_items(run_id: int, db: Session = Depends(get_db)):
    if not db.get(PayrollRun, run_id):
        raise HTTPException(status_code=404, detail="Payroll run not found")
    return (
        db.query(PayrollRunLineItem)
        .filter(PayrollRunLineItem.payroll_run_id == run_id)
        .order_by(PayrollRunLineItem.employee_id)
        .all()
    )


@router.get("/payroll-runs/{run_id}/line-items/{employee_id}", response_model=PayrollRunLineItemRead)
def get_payroll_run_line_item_for_employee(run_id: int, employee_id: int, db: Session = Depends(get_db)):
    item = (
        db.query(PayrollRunLineItem)
        .filter(PayrollRunLineItem.payroll_run_id == run_id, PayrollRunLineItem.employee_id == employee_id)
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Line item not found for this employee in this run")
    return item


@router.post("/payroll-runs/{run_id}/calculate", response_model=PayrollRunRead)
def calculate_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status not in ("Draft", "Calculated", "Failed"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot calculate a run in status '{run.status}'.",
        )
    try:
        calculate_payroll_run(db, run)
    except Exception:  # noqa: BLE001 - engine already persisted status=Failed + error_message on the run
        pass
    db.refresh(run)
    return run


@router.post("/payroll-runs/{run_id}/approve", response_model=PayrollRunRead)
def approve_run(run_id: int, payload: PayrollRunApprove, db: Session = Depends(get_db)):
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status not in ("Calculated", "PendingApproval"):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot approve a run in status '{run.status}' — it must be Calculated or PendingApproval first.",
        )
    run.status = "Approved"
    run.approved_at = datetime.now(timezone.utc)
    run.approved_by = payload.approved_by
    db.commit()
    db.refresh(run)
    return run


@router.post("/payroll-runs/{run_id}/cancel", response_model=PayrollRunRead)
def cancel_run(run_id: int, db: Session = Depends(get_db)):
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status in ("Paid", "Cancelled"):
        raise HTTPException(status_code=400, detail=f"Cannot cancel a run in status '{run.status}'.")
    run.status = "Cancelled"
    db.commit()
    db.refresh(run)
    return run
