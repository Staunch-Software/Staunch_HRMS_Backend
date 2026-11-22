import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.disbursement import DisbursementBatch, DisbursementBatchItem
from app.models.pay_group import PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.user import AdminUser
from app.schemas.payroll_run import (
    PayrollRunApprove,
    PayrollRunCreate,
    PayrollRunLineItemRead,
    PayrollRunRead,
)
from app.services.approval_settings import maker_checker_enabled
from app.services.payroll_engine.engine import calculate_payroll_run

router = APIRouter(tags=["payroll-runs"], dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)
BACKEND_DIR = Path(__file__).resolve().parents[3]


@router.post("/payroll-runs", response_model=PayrollRunRead, status_code=status.HTTP_201_CREATED)
def create_payroll_run(payload: PayrollRunCreate, db: Session = Depends(get_db)):
    if not db.get(PaySchedule, payload.pay_schedule_id):
        raise HTTPException(status_code=404, detail="Pay schedule not found")
    if payload.pay_group_id is not None:
        group = db.get(PayGroup, payload.pay_group_id)
        if not group:
            raise HTTPException(status_code=404, detail="Pay group not found")
        if not group.is_active:
            raise HTTPException(status_code=400, detail=f"Pay group {group.code} is inactive")
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
def calculate_run(run_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(get_current_user)):
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
    run.calculated_by = user.username
    run.submitted_by = None
    run.submitted_at = None
    db.commit()
    db.refresh(run)
    return run


@router.post("/payroll-runs/{run_id}/submit", response_model=PayrollRunRead)
def submit_run(run_id: int, db: Session = Depends(get_db), user: AdminUser = Depends(get_current_user)):
    """Maker-checker: Calculated -> PendingApproval."""
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status != "Calculated":
        raise HTTPException(status_code=400,
                            detail=f"Only a Calculated run can be submitted for approval (status '{run.status}').")
    run.status = "PendingApproval"
    run.submitted_by = user.username
    run.submitted_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(run)
    return run


@router.post("/payroll-runs/{run_id}/approve", response_model=PayrollRunRead)
def approve_run(run_id: int, payload: PayrollRunApprove, db: Session = Depends(get_db),
                user: AdminUser = Depends(get_current_user)):
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if maker_checker_enabled(db):
        if run.status != "PendingApproval":
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve a run in status '{run.status}': submit it for approval first.",
            )
        if user.username in (run.calculated_by, run.submitted_by):
            raise HTTPException(
                status_code=403,
                detail="Maker-checker is on: a different HR user must approve the run you calculated or submitted.",
            )
        approver = user.username
    else:
        if run.status not in ("Calculated", "PendingApproval"):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot approve a run in status '{run.status}' — it must be Calculated or PendingApproval first.",
            )
        approver = payload.approved_by or user.username
    run.status = "Approved"
    run.approved_at = datetime.now(timezone.utc)
    run.approved_by = approver
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


@router.delete("/payroll-runs/{run_id}")
def delete_run(run_id: int, user: AdminUser = Depends(get_current_user), db: Session = Depends(get_db)):
    """Deletes a run with its line items and any bank files that were only
    generated. Paid runs and runs whose bank file went to the bank stay."""
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status == "Paid":
        raise HTTPException(status_code=409, detail="A paid payroll run cannot be deleted.")
    batches = db.query(DisbursementBatch).filter(DisbursementBatch.payroll_run_id == run.id).all()
    sent = [b for b in batches if b.status != "Generated" or b.icici_submitted_at is not None]
    if sent:
        raise HTTPException(
            status_code=409,
            detail="This run has a bank file that was already submitted to the bank, so it cannot be deleted.",
        )
    files = [b.file_path for b in batches if b.file_path]
    for batch in batches:
        db.query(DisbursementBatchItem).filter(DisbursementBatchItem.disbursement_batch_id == batch.id).delete(
            synchronize_session=False)
        db.delete(batch)
    db.query(PayrollRunLineItem).filter(PayrollRunLineItem.payroll_run_id == run.id).delete(synchronize_session=False)
    summary = {"deleted": run.id, "status": run.status, "bank_files_removed": len(batches)}
    db.delete(run)
    db.commit()
    for rel in files:
        path = (BACKEND_DIR / rel).resolve()
        if BACKEND_DIR in path.parents and path.is_file():
            try:
                path.unlink()
            except OSError:
                logger.warning("Could not remove bank file %s of deleted run %s", path, run_id)
    logger.info("Payroll run %s (%s) deleted by %s", run_id, summary["status"], user.username)
    return summary
