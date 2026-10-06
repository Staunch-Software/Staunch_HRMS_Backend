"""Server-side Excel upload for payroll runs (Phase 3) — two independent
modes:

  * variable-inputs: bulk-fills `variable_pay_inputs` (attendance/LOP/OT/
    bonus/ad-hoc deduction), same data manual entry (VariableInputsForm.js)
    already writes — the run still needs a separate "Calculate" afterwards.
  * final-sheet: bulk-writes `payroll_run_line_items` directly from an
    externally-computed sheet, skipping the calc engine entirely and marking
    the run `source="ExcelFinalUpload"`.

Both follow the same two-phase upload -> preview -> commit flow: parsing
happens server-side (pandas/openpyxl) because validation needs DB lookups
(active employees in the run's scope, the run's own period) a browser can't
do on its own, and because server-side parsing gives identical results
regardless of who uploads. Nothing is written to the real tables until
"commit", and commit re-validates the batch from scratch against current DB
state rather than trusting the client's "looked fine at preview time" state.
"""
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from io import BytesIO
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.salary_component import SalaryComponent
from app.models.variable_pay import VariablePayInput
from app.schemas.excel_upload import (
    BatchCommitRequest,
    FinalSheetCommitResponse,
    UploadPreviewResponse,
    VariableInputsCommitResponse,
)
from app.services.excel_import import batch_store
from app.services.excel_import.excel_reader import ExcelParseError, read_workbook_as_records
from app.services.excel_import.final_sheet_parser import validate_final_sheet_records
from app.services.excel_import.templates import build_final_sheet_template, build_variable_inputs_template
from app.services.excel_import.variable_inputs_parser import (
    ADHOC_DEDUCTION_COMPONENT_CODE,
    BONUS_COMPONENT_CODE,
    validate_variable_inputs_records,
)
from app.services.payroll_engine.engine import _employee_snapshot, _resolve_employee_scope  # noqa: WPS437

router = APIRouter(tags=["payroll-run-uploads"], dependencies=[Depends(get_current_user)])

# Run statuses from which a fresh Excel commit (either mode) is still safe —
# mirrors the same guard POST /payroll-runs/{id}/calculate already applies.
COMMITTABLE_RUN_STATUSES = ("Draft", "Calculated", "Failed")


def _get_run_or_404(db: Session, run_id: int) -> PayrollRun:
    run = db.get(PayrollRun, run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    return run


def _active_component_codes(db: Session) -> list[str]:
    return [
        c.code
        for c in db.query(SalaryComponent).filter(SalaryComponent.status.is_(True)).order_by(SalaryComponent.code).all()
    ]


def _preview_response(batch_id: str, results: list) -> dict:
    error_count = sum(1 for r in results if r.errors)
    warning_count = sum(1 for r in results if r.warnings)
    return {
        "batch_id": batch_id,
        "rows": [r.to_response() for r in results],
        "summary": {
            "total_rows": len(results),
            "error_count": error_count,
            "warning_count": warning_count,
        },
    }


def _xlsx_response(content: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        BytesIO(content),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- Variable inputs ---------------------------------------------------------


@router.get("/payroll-runs/{run_id}/variable-inputs/template")
def download_variable_inputs_template(run_id: int, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    employees = _resolve_employee_scope(db, run)
    content = build_variable_inputs_template(run, employees)
    filename = f"variable_inputs_template_run{run_id}_{run.period_start.strftime('%Y%m')}.xlsx"
    return _xlsx_response(content, filename)


@router.post("/payroll-runs/{run_id}/variable-inputs/upload", response_model=UploadPreviewResponse)
async def upload_variable_inputs(run_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    file_bytes = await file.read()
    try:
        records = read_workbook_as_records(file_bytes)
        results = validate_variable_inputs_records(db, run, records)
    except ExcelParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch = batch_store.create(run_id=run_id, mode="variable-inputs", raw_rows=records)
    return _preview_response(batch.batch_id, results)


@router.post("/payroll-runs/{run_id}/variable-inputs/commit", response_model=VariableInputsCommitResponse)
def commit_variable_inputs(run_id: int, payload: BatchCommitRequest, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    batch = batch_store.get(payload.batch_id)
    if not batch or batch.run_id != run_id or batch.mode != "variable-inputs":
        raise HTTPException(
            status_code=404,
            detail="Upload batch not found or has expired — please re-upload the file.",
        )

    try:
        results = validate_variable_inputs_records(db, run, batch.raw_rows)
    except ExcelParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    error_rows = [r for r in results if r.errors]
    if error_rows:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This batch still has blocking row errors and cannot be committed.",
                "rows": [r.to_response() for r in error_rows],
            },
        )

    employee_ids = [r.data["employee_id"] for r in results]

    locked_exists = (
        db.query(VariablePayInput)
        .filter(
            VariablePayInput.employee_id.in_(employee_ids),
            VariablePayInput.period_start == run.period_start,
            VariablePayInput.period_end == run.period_end,
            VariablePayInput.status == "Locked",
        )
        .first()
    )
    if locked_exists:
        raise HTTPException(
            status_code=409,
            detail=(
                "Variable inputs for this period are Locked — a payroll run has already "
                "consumed them. They cannot be replaced by this upload."
            ),
        )

    # Atomic replace: delete existing rows for this run's employees/period,
    # then insert the batch fresh — never accumulates duplicates, matching
    # the manual-entry upsert's replace semantics.
    db.query(VariablePayInput).filter(
        VariablePayInput.employee_id.in_(employee_ids),
        VariablePayInput.period_start == run.period_start,
        VariablePayInput.period_end == run.period_end,
    ).delete(synchronize_session=False)

    import_batch_id = str(uuid4())
    for result in results:
        data = result.data
        ad_hoc_earnings = []
        if (data["bonus_amount"] or 0) > 0:
            ad_hoc_earnings.append(
                {
                    "component_code": BONUS_COMPONENT_CODE,
                    "amount": data["bonus_amount"],
                    "remarks": data["bonus_reason"],
                }
            )
        ad_hoc_deductions = []
        if (data["adhoc_deduction_amount"] or 0) > 0:
            ad_hoc_deductions.append(
                {
                    "component_code": ADHOC_DEDUCTION_COMPONENT_CODE,
                    "amount": data["adhoc_deduction_amount"],
                    "remarks": data["adhoc_deduction_reason"],
                }
            )

        db.add(
            VariablePayInput(
                employee_id=data["employee_id"],
                period_start=run.period_start,
                period_end=run.period_end,
                source="ExcelImport",
                status="Draft",
                present_days=data["days_present"],
                lop_days=data["lop_days"] or 0,
                overtime_hours=data["overtime_hours"] or 0,
                ad_hoc_earnings=ad_hoc_earnings,
                ad_hoc_deductions=ad_hoc_deductions,
                remarks=data["remarks"],
                import_batch_id=import_batch_id,
            )
        )

    db.commit()
    batch_store.delete(payload.batch_id)

    return {"batch_id": payload.batch_id, "committed_rows": len(results), "import_batch_id": import_batch_id}


# --- Final sheet --------------------------------------------------------------


@router.get("/payroll-runs/{run_id}/final-sheet/template")
def download_final_sheet_template(run_id: int, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    employees = _resolve_employee_scope(db, run)
    component_codes = _active_component_codes(db)
    content = build_final_sheet_template(run, employees, component_codes)
    filename = f"final_sheet_template_run{run_id}_{run.period_start.strftime('%Y%m')}.xlsx"
    return _xlsx_response(content, filename)


@router.post("/payroll-runs/{run_id}/final-sheet/upload", response_model=UploadPreviewResponse)
async def upload_final_sheet(run_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    component_codes = _active_component_codes(db)
    file_bytes = await file.read()
    try:
        records = read_workbook_as_records(file_bytes)
        results = validate_final_sheet_records(db, run, records, component_codes)
    except ExcelParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch = batch_store.create(run_id=run_id, mode="final-sheet", raw_rows=records)
    return _preview_response(batch.batch_id, results)


@router.post("/payroll-runs/{run_id}/final-sheet/commit", response_model=FinalSheetCommitResponse)
def commit_final_sheet(run_id: int, payload: BatchCommitRequest, db: Session = Depends(get_db)):
    run = _get_run_or_404(db, run_id)
    if run.status not in COMMITTABLE_RUN_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot import a final sheet for a run in status '{run.status}'.",
        )

    batch = batch_store.get(payload.batch_id)
    if not batch or batch.run_id != run_id or batch.mode != "final-sheet":
        raise HTTPException(
            status_code=404,
            detail="Upload batch not found or has expired — please re-upload the file.",
        )

    component_codes = _active_component_codes(db)
    component_by_code = {c.code: c for c in db.query(SalaryComponent).filter(SalaryComponent.code.in_(component_codes)).all()}

    try:
        results = validate_final_sheet_records(db, run, batch.raw_rows, component_codes)
    except ExcelParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    error_rows = [r for r in results if r.errors]
    if error_rows:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "This batch still has blocking row errors and cannot be committed.",
                "rows": [r.to_response() for r in error_rows],
            },
        )

    employees_by_id = {emp.id: emp for emp in _resolve_employee_scope(db, run)}

    db.query(PayrollRunLineItem).filter(PayrollRunLineItem.payroll_run_id == run.id).delete(synchronize_session=False)

    total_gross = Decimal("0")
    total_deductions = Decimal("0")
    total_net = Decimal("0")

    for result in results:
        data = result.data
        employee = employees_by_id[data["employee_id"]]
        net_pay = Decimal(str(data["net_pay"]))
        gross = data["gross_earnings"]
        deductions = data["total_deductions"]

        if gross is None and deductions is None:
            gross_dec, deductions_dec = net_pay, Decimal("0")
        elif gross is None:
            deductions_dec = Decimal(str(deductions))
            gross_dec = net_pay + deductions_dec
        elif deductions is None:
            gross_dec = Decimal(str(gross))
            deductions_dec = gross_dec - net_pay
        else:
            gross_dec = Decimal(str(gross))
            deductions_dec = Decimal(str(deductions))

        breakdown = []
        for code, amount in (data["components"] or {}).items():
            component = component_by_code.get(code)
            breakdown.append(
                {
                    "component_code": code,
                    "component_name": component.name if component else code,
                    "component_type": component.component_type if component else None,
                    "calculation_method": "ExcelImport",
                    "formula_or_value": None,
                    "resolved_amount": float(amount),
                }
            )
        if data.get("remarks"):
            breakdown.append(
                {
                    "component_code": "_SYSTEM_NOTE",
                    "component_name": "Remarks",
                    "component_type": "Info",
                    "calculation_method": None,
                    "formula_or_value": None,
                    "resolved_amount": 0.0,
                    "warning": data["remarks"],
                }
            )

        db.add(
            PayrollRunLineItem(
                payroll_run_id=run.id,
                employee_id=employee.id,
                employee_snapshot=_employee_snapshot(db, employee),
                gross_earnings=float(gross_dec),
                total_deductions=float(deductions_dec),
                total_reimbursements=0.0,
                net_pay=float(net_pay),
                status="Included",
                computation_breakdown=breakdown,
                error_message=None,
            )
        )
        total_gross += gross_dec
        total_deductions += deductions_dec
        total_net += net_pay

    run.source = "ExcelFinalUpload"
    run.status = "Calculated"
    run.calculated_at = datetime.now(timezone.utc)
    run.error_message = None
    run.total_gross = float(total_gross)
    run.total_deductions = float(total_deductions)
    run.total_net = float(total_net)
    run.employee_count = len(results)

    db.commit()
    db.refresh(run)
    batch_store.delete(payload.batch_id)

    return {"committed_rows": len(results), "run": run}
