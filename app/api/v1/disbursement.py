"""Phase 4 (final phase): bank disbursement batch generation/history/
download for Approved payroll runs, plus the company debit-account config
that every generated file's "Debit Account Number" column comes from.

Reminder carried into every response that touches it: `generic_neft_xlsx_v1`
is a placeholder format (see app/services/bank_file/generic_neft_xlsx_v1.py)
and the seeded company_bank_config row is a fake placeholder — neither is
verified/real, both must be replaced before an actual disbursement.
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.company_bank_config import CompanyBankConfig
from app.models.disbursement import DisbursementBatch
from app.models.employee import EmployeeBankAccount
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.schemas.company_bank_config import CompanyBankConfigRead, CompanyBankConfigUpdate
from app.schemas.disbursement import DisbursementBatchCreate, DisbursementBatchRead, DisbursementDataRow
from app.services.bank_file.generate import GENERATED_FILES_DIR, generate_bank_file
from app.services.bank_file.registry import get_media_type

router = APIRouter(tags=["disbursement"], dependencies=[Depends(get_current_user)])


def _get_run_or_404(db: Session, run_id: int) -> PayrollRun:
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    return run


def _batch_to_read(batch: DisbursementBatch) -> dict:
    return {
        "id": batch.id,
        "payroll_run_id": batch.payroll_run_id,
        "format_key": batch.format_key,
        "file_name": batch.file_name,
        "status": batch.status,
        "total_amount": float(batch.total_amount),
        "total_employee_count": batch.total_employee_count,
        "generated_at": batch.generated_at,
        "generated_by": batch.generated_by,
        "version": batch.version,
        "checksum": batch.checksum,
        "download_url": f"/disbursement-batches/{batch.id}/download",
        "icici_file_sequence_num": batch.icici_file_sequence_num,
        "icici_submitted_at": batch.icici_submitted_at,
        "icici_message": batch.icici_message,
        "icici_otp_pending": bool(batch.icici_pending_unique_id),
    }


# --- Disbursement batches -----------------------------------------------------


@router.post(
    "/payroll-runs/{run_id}/disbursement-batches",
    response_model=DisbursementBatchRead,
    status_code=status.HTTP_201_CREATED,
)
def create_disbursement_batch(run_id: int, payload: DisbursementBatchCreate, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    try:
        result = generate_bank_file(db, run, format_key=payload.format_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if result.blocking_issues:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Bank file generation blocked — fix these employees' bank details first.",
                "issues": result.blocking_issues,
                "warnings": result.warnings,
            },
        )

    return _batch_to_read(result.batch)


@router.get("/payroll-runs/{run_id}/disbursement-batches", response_model=list[DisbursementBatchRead])
def list_disbursement_batches(run_id: int, db: Session = Depends(get_db)):
    _get_run_or_404(db, run_id)
    batches = (
        db.query(DisbursementBatch)
        .filter(DisbursementBatch.payroll_run_id == run_id)
        .order_by(DisbursementBatch.version.desc())
        .all()
    )
    return [_batch_to_read(b) for b in batches]


@router.get("/disbursement-batches/{batch_id}/download")
def download_disbursement_batch(batch_id: int, db: Session = Depends(get_db)):
    batch = db.get(DisbursementBatch, batch_id)
    if not batch:
        raise HTTPException(status_code=404, detail="Disbursement batch not found")

    file_path: Path = GENERATED_FILES_DIR.parent / batch.file_path
    if not file_path.exists():
        raise HTTPException(status_code=410, detail="The generated file is no longer available on disk.")

    # Streams the exact bytes written at generation time — never regenerated
    # here, so this is guaranteed byte-for-byte identical to the original.
    return FileResponse(
        path=str(file_path),
        media_type=get_media_type(batch.format_key),
        filename=batch.file_name,
    )


@router.get("/payroll-runs/{run_id}/disbursement-data", response_model=list[DisbursementDataRow])
def get_disbursement_data(run_id: int, db: Session = Depends(get_db)):
    """Read-only inspection endpoint — the same contract point
    `generate_bank_file` itself uses internally, exposed independently so a
    future bank-file agent (or a manual reviewer) can inspect what would be
    disbursed without triggering file generation."""
    run = _get_run_or_404(db, run_id)
    if run.status != "Approved":
        raise HTTPException(
            status_code=400,
            detail=f"Run must be Approved to view disbursement data (current status: '{run.status}').",
        )

    line_items = (
        db.query(PayrollRunLineItem)
        .filter(PayrollRunLineItem.payroll_run_id == run_id, PayrollRunLineItem.status == "Included")
        .order_by(PayrollRunLineItem.employee_id)
        .all()
    )

    rows = []
    for line_item in line_items:
        snapshot = line_item.employee_snapshot or {}
        bank = (
            db.query(EmployeeBankAccount)
            .filter(EmployeeBankAccount.employee_id == line_item.employee_id, EmployeeBankAccount.is_active.is_(True))
            .order_by(EmployeeBankAccount.id.desc())
            .first()
        )
        rows.append(
            {
                "employee_id": line_item.employee_id,
                "employee_code": snapshot.get("employee_code") or str(line_item.employee_id),
                "name": snapshot.get("name") or f"Employee #{line_item.employee_id}",
                "bank_name": bank.bank_name if bank else None,
                "account_number": bank.account_number if bank else None,
                "ifsc_code": bank.ifsc_code if bank else None,
                "net_pay": float(line_item.net_pay),
            }
        )
    return rows


# --- Company bank (debit account) config --------------------------------------


@router.get("/company-bank-config", response_model=CompanyBankConfigRead)
def get_company_bank_config(db: Session = Depends(get_db)):
    config = (
        db.query(CompanyBankConfig)
        .filter(CompanyBankConfig.is_active.is_(True))
        .order_by(CompanyBankConfig.id.desc())
        .first()
    )
    if not config:
        raise HTTPException(status_code=404, detail="No active company bank config found.")
    return config


@router.put("/company-bank-config", response_model=CompanyBankConfigRead)
def upsert_company_bank_config(payload: CompanyBankConfigUpdate, db: Session = Depends(get_db)):
    config = (
        db.query(CompanyBankConfig)
        .filter(CompanyBankConfig.is_active.is_(True))
        .order_by(CompanyBankConfig.id.desc())
        .first()
    )
    if config:
        for field_name, value in payload.model_dump().items():
            setattr(config, field_name, value)
    else:
        config = CompanyBankConfig(**payload.model_dump())
        db.add(config)
    db.commit()
    db.refresh(config)
    return config
