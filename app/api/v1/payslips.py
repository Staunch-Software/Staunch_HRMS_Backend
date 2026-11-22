"""Payslip PDFs. `router` (HR): one employee's payslip or a ZIP of the whole
run. `me_router` (employee portal): the caller's payslips from runs released
per Payslip Settings (Approved or Paid / Paid only)."""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from app.api.v1.me import get_current_employee
from app.core.db import get_db
from app.core.security import require_employee_user
from app.models.employee import Employee
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.schemas.payroll_settings import MyPayslipRow
from app.services.payroll_settings import payslip_settings
from app.services.payslip.data import build_payslip, pdf_password, released_statuses
from app.services.payslip.render import (
    PayslipRenderError,
    logo_data_uri,
    payslip_filename,
    render_pdf,
    safe_filename,
    zip_files,
)

router = APIRouter(tags=["payslips"])
me_router = APIRouter(prefix="/me", tags=["self-service"], dependencies=[Depends(require_employee_user)])

PAYSLIP_RUN_STATUSES = ("Calculated", "PendingApproval", "Approved", "Paid")


def _pdf_response(content: bytes, filename: str, media_type: str = "application/pdf") -> Response:
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _run_for_payslips(db: Session, run_id: int) -> PayrollRun:
    run = db.get(PayrollRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Payroll run not found")
    if run.status not in PAYSLIP_RUN_STATUSES:
        raise HTTPException(status_code=409, detail=f"Payslips are available once the run is calculated (status {run.status}).")
    return run


def _included_item(db: Session, run_id: int, employee_id: int) -> PayrollRunLineItem:
    item = db.query(PayrollRunLineItem).filter(
        PayrollRunLineItem.payroll_run_id == run_id, PayrollRunLineItem.employee_id == employee_id,
        PayrollRunLineItem.status == "Included",
    ).first()
    if item is None:
        raise HTTPException(status_code=404, detail="No payslip for this employee in this run")
    return item


def _render(db: Session, run: PayrollRun, item: PayrollRunLineItem, settings, logo: str, password=None) -> tuple[str, bytes]:
    payslip = build_payslip(db, run, item, settings, logo)
    try:
        return payslip_filename(payslip), render_pdf(payslip, settings.template, password)
    except PayslipRenderError as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@router.get("/payroll-runs/{run_id}/payslips/{employee_id}.pdf")
def download_payslip(run_id: int, employee_id: int, db: Session = Depends(get_db)):
    run = _run_for_payslips(db, run_id)
    item = _included_item(db, run_id, employee_id)
    settings = payslip_settings(db)
    filename, pdf = _render(db, run, item, settings, logo_data_uri(settings))
    return _pdf_response(pdf, filename)


@router.get("/payroll-runs/{run_id}/payslips.zip")
def download_run_payslips(run_id: int, db: Session = Depends(get_db)):
    run = _run_for_payslips(db, run_id)
    items = (
        db.query(PayrollRunLineItem)
        .filter(PayrollRunLineItem.payroll_run_id == run_id, PayrollRunLineItem.status == "Included")
        .order_by(PayrollRunLineItem.id)
        .all()
    )
    if not items:
        raise HTTPException(status_code=404, detail="This run has no included employees")
    settings = payslip_settings(db)
    logo = logo_data_uri(settings)
    files = [_render(db, run, item, settings, logo) for item in items]
    name = safe_filename(f"Payslips_run{run.id}_{run.period_end:%b_%Y}") + ".zip"
    return _pdf_response(zip_files(files), name, "application/zip")


def _released_runs(db: Session, employee_id: int, settings):
    return (
        db.query(PayrollRun, PayrollRunLineItem)
        .join(PayrollRunLineItem, PayrollRunLineItem.payroll_run_id == PayrollRun.id)
        .filter(
            PayrollRunLineItem.employee_id == employee_id,
            PayrollRunLineItem.status == "Included",
            PayrollRun.status.in_(released_statuses(settings)),
        )
    )


@me_router.get("/payslips", response_model=list[MyPayslipRow])
def my_payslips(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    settings = payslip_settings(db)
    rows = _released_runs(db, employee.id, settings).order_by(PayrollRun.period_end.desc(), PayrollRun.id.desc()).all()
    return [
        MyPayslipRow(
            run_id=run.id, period_start=run.period_start, period_end=run.period_end, pay_date=run.pay_date,
            month_label=run.period_end.strftime("%B %Y"), status=run.status,
            gross_earnings=float(item.gross_earnings or 0), total_deductions=float(item.total_deductions or 0),
            net_pay=float(item.net_pay or 0),
        )
        for run, item in rows
    ]


@me_router.get("/payslips/{run_id}.pdf")
def my_payslip_pdf(run_id: int, employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    settings = payslip_settings(db)
    row = _released_runs(db, employee.id, settings).filter(PayrollRun.id == run_id).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Payslip not found")
    run, item = row
    filename, pdf = _render(db, run, item, settings, logo_data_uri(settings), pdf_password(settings, employee))
    return _pdf_response(pdf, filename)
