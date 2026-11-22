"""Payslip content for one employee in one payroll run: the run line item's
breakdown (lines shown on the payslip) and employee_snapshot, plus the
month's attendance, leave balances and year-to-date totals, filtered by the
payslip settings."""
import calendar
from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.core.crypto import mask_pan
from app.models.attendance_month import AttendanceMonthResult
from app.models.employee import Employee
from app.models.leave import LeaveType
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.payroll_settings import RELEASE_PAID
from app.services.payslip.words import indian_amount, rupees_in_words

ZERO = Decimal("0")
EARNING_TYPES = ("Earning", "Reimbursement")
DEDUCTION_TYPES = ("Deduction",)
EMPLOYER_TYPES = ("EmployerCost", "EmployerContribution")
YTD_STATUSES = ("Approved", "Paid")


def released_statuses(settings) -> tuple[str, ...]:
    return ("Paid",) if settings.release_on == RELEASE_PAID else ("Approved", "Paid")


def _d(value) -> Decimal:
    if value in (None, ""):
        return ZERO
    return Decimal(str(value))


def _fy_start(day: date) -> date:
    return date(day.year if day.month >= 4 else day.year - 1, 4, 1)


def _fmt_days(value) -> Optional[str]:
    if value is None:
        return None
    return format(_d(value).normalize(), "f")


def _payslip_lines(breakdown: list) -> tuple[list, list, list]:
    earnings, deductions, employer = [], [], []
    for entry in breakdown or []:
        if entry.get("show_on_payslip") is False:
            continue
        code = entry.get("component_code") or ""
        if code.startswith("_"):
            continue
        amount = _d(entry.get("resolved_amount"))
        if amount == 0:
            continue
        kind = entry.get("component_type")
        row = {"code": code, "name": entry.get("component_name") or code, "amount": amount}
        if kind in EARNING_TYPES:
            earnings.append(row)
        elif kind in DEDUCTION_TYPES:
            deductions.append(row)
        elif kind in EMPLOYER_TYPES:
            employer.append(row)
    return earnings, deductions, employer


def _ytd(db: Session, run: PayrollRun, employee_id: int) -> tuple[dict, dict]:
    """(per component code totals, overall totals) for the financial year up
    to and including this run's month: approved/paid runs plus this run."""
    items = (
        db.query(PayrollRunLineItem)
        .join(PayrollRun, PayrollRunLineItem.payroll_run_id == PayrollRun.id)
        .filter(
            PayrollRunLineItem.employee_id == employee_id,
            PayrollRunLineItem.status == "Included",
            PayrollRun.period_end >= _fy_start(run.period_end),
            PayrollRun.period_end <= run.period_end,
            (PayrollRun.status.in_(YTD_STATUSES)) | (PayrollRun.id == run.id),
        )
        .all()
    )
    by_code: dict[str, Decimal] = defaultdict(lambda: ZERO)
    totals = {"gross": ZERO, "deductions": ZERO, "net": ZERO}
    for item in items:
        totals["gross"] += _d(item.gross_earnings)
        totals["deductions"] += _d(item.total_deductions)
        totals["net"] += _d(item.net_pay)
        earnings, deductions, employer = _payslip_lines(item.computation_breakdown)
        for row in earnings + deductions + employer:
            by_code[row["code"]] += row["amount"]
    return by_code, totals


def _attendance(db: Session, run: PayrollRun, item: PayrollRunLineItem) -> dict:
    days_in_month = calendar.monthrange(run.period_end.year, run.period_end.month)[1]
    month = db.query(AttendanceMonthResult).filter(
        AttendanceMonthResult.employee_id == item.employee_id,
        AttendanceMonthResult.month == run.period_end.strftime("%Y-%m"),
    ).first()
    totals = month.totals if month else {}
    return {
        "days_in_month": days_in_month,
        "working_days": totals.get("working_days"),
        "paid_days": _fmt_days(item.paid_days),
        "lop_days": _fmt_days(item.lop_days if item.lop_days is not None else (month.lop_days if month else None)),
        "present_days": _fmt_days(month.present_days) if month else None,
        "paid_leave_days": _fmt_days(month.paid_leave_days) if month else None,
    }


def _leave_balances(db: Session, employee_id: int, on: date) -> list[dict]:
    from app.services.leave import balance as balance_service

    year = balance_service.leave_year_for(db, on)
    summaries = balance_service.balance_summaries(db, year, [employee_id])
    rows = []
    types = db.query(LeaveType).filter(LeaveType.is_active.is_(True)).order_by(LeaveType.sort_order, LeaveType.code)
    for leave_type in types:
        summary = summaries.get((employee_id, leave_type.id))
        if summary is None:
            continue
        rows.append({
            "code": leave_type.code, "name": leave_type.name,
            "credited": _fmt_days(summary["credited"] + summary["carried_forward"] + summary["adjusted"]),
            "used": _fmt_days(summary["used"]),
            "balance": _fmt_days(summary["balance"]),
        })
    return rows


def _side_by_side(earnings: list, deductions: list) -> list[tuple]:
    size = max(len(earnings), len(deductions))
    return [(earnings[i] if i < len(earnings) else None, deductions[i] if i < len(deductions) else None)
            for i in range(size)]


def _company(settings, logo_uri: Optional[str]) -> dict:
    address = [ln.strip() for ln in (settings.company_address or "").splitlines() if ln.strip()]
    return {"name": settings.company_name, "address": address, "cin": settings.company_cin,
            "gstin": settings.company_gstin, "logo": logo_uri}


def _flags(settings) -> dict:
    return {key: bool(getattr(settings, key)) for key in (
        "show_pan", "show_uan", "show_bank", "show_attendance", "show_leave_balance", "show_ytd",
        "show_employer_contrib")}


def assemble(settings, logo_uri: Optional[str], period_start: date, period_end: date, pay_date: date, employee: dict,
             earnings: list, deductions: list, employer: list, gross, total_deductions, net,
             attendance: Optional[dict], leave: list, ytd_by_code: dict, ytd_totals: Optional[dict],
             run_label: Optional[str] = None) -> dict:
    flags = _flags(settings)
    for row in earnings + deductions + employer:
        row["amount_text"] = indian_amount(row["amount"])
        ytd = ytd_by_code.get(row["code"])
        row["ytd_text"] = indian_amount(ytd) if ytd is not None else ""
    if not flags["show_pan"]:
        employee["pan"] = None
    if not flags["show_uan"]:
        employee["uan"] = None
    if not flags["show_bank"]:
        employee["bank_name"] = employee["account"] = employee["ifsc"] = None
    fields = [
        ("Employee name", employee.get("name")), ("Employee code", employee.get("code")),
        ("Designation", employee.get("designation")), ("Department", employee.get("department")),
        ("Location", employee.get("location")), ("Date of joining", employee.get("date_of_joining")),
        ("PAN", employee.get("pan")), ("UAN", employee.get("uan")),
        ("Bank", employee.get("bank_name")), ("Account no.", employee.get("account")),
        ("Pay date", pay_date.strftime("%d %b %Y")),
        ("Pay period", f"{period_start:%d %b %Y} - {period_end:%d %b %Y}"),
    ]
    return {
        "company": _company(settings, logo_uri),
        "employee_fields": [(label, value) for label, value in fields if value or label.startswith("Employee")],
        "month_label": period_end.strftime("%B %Y"),
        "period": {"start": period_start.strftime("%d %b %Y"), "end": period_end.strftime("%d %b %Y"),
                   "pay_date": pay_date.strftime("%d %b %Y")},
        "run_label": run_label,
        "employee": employee,
        "earnings": earnings,
        "deductions": deductions,
        "rows": _side_by_side(earnings, deductions),
        "employer": employer if flags["show_employer_contrib"] else [],
        "totals": {
            "gross": indian_amount(gross), "deductions": indian_amount(total_deductions), "net": indian_amount(net),
            "net_words": rupees_in_words(net),
        },
        "attendance": attendance if flags["show_attendance"] else None,
        "leave": leave if flags["show_leave_balance"] else [],
        "ytd": ({k: indian_amount(v) for k, v in ytd_totals.items()} if flags["show_ytd"] and ytd_totals else None),
        "show_ytd_column": flags["show_ytd"],
        "footer_note": settings.footer_note,
        "flags": flags,
    }


def employee_block(snapshot: dict, employee: Optional[Employee]) -> dict:
    return {
        "name": snapshot.get("name") or (f"{employee.first_name} {employee.last_name or ''}".strip() if employee else ""),
        "code": snapshot.get("employee_code") or (employee.employee_code if employee else ""),
        "department": snapshot.get("department"),
        "designation": snapshot.get("designation"),
        "location": snapshot.get("work_location"),
        "date_of_joining": employee.date_of_joining.strftime("%d %b %Y") if employee and employee.date_of_joining else None,
        "pan": mask_pan(employee.pan_number) if employee and employee.pan_number else None,
        "uan": employee.uan_number if employee else None,
        "bank_name": snapshot.get("bank_name"),
        "account": snapshot.get("account_number_masked"),
        "ifsc": snapshot.get("ifsc_code"),
    }


def build_payslip(db: Session, run: PayrollRun, item: PayrollRunLineItem, settings, logo_uri: Optional[str]) -> dict:
    employee = db.get(Employee, item.employee_id)
    earnings, deductions, employer = _payslip_lines(item.computation_breakdown)
    flags = _flags(settings)
    ytd_by_code, ytd_totals = _ytd(db, run, item.employee_id) if flags["show_ytd"] else ({}, None)
    leave = _leave_balances(db, item.employee_id, run.period_end) if flags["show_leave_balance"] else []
    return assemble(
        settings, logo_uri, run.period_start, run.period_end, run.pay_date,
        employee_block(item.employee_snapshot or {}, employee), earnings, deductions, employer,
        item.gross_earnings, item.total_deductions, item.net_pay,
        _attendance(db, run, item), leave, ytd_by_code, ytd_totals, run_label=f"Run #{run.id}",
    )


def sample_payslip(settings, logo_uri: Optional[str]) -> dict:
    """Illustrative data for the settings preview."""
    earnings = [
        {"code": "BASIC_DA", "name": "Basic & DA", "amount": Decimal("16621")},
        {"code": "HRA", "name": "HRA", "amount": Decimal("7123")},
        {"code": "SPL_ALLOWANCE", "name": "Special allowance", "amount": Decimal("5936")},
        {"code": "CONVEYANCE", "name": "Conveyance", "amount": Decimal("1600")},
    ]
    deductions = [
        {"code": "PF_EE", "name": "Employee PF", "amount": Decimal("1995")},
        {"code": "PT", "name": "Professional tax", "amount": Decimal("208")},
        {"code": "TDS", "name": "Income tax (TDS)", "amount": Decimal("1250")},
    ]
    employer = [{"code": "PF_ER", "name": "Employer PF", "amount": Decimal("2316")}]
    gross = sum((r["amount"] for r in earnings), ZERO)
    total_ded = sum((r["amount"] for r in deductions), ZERO)
    ytd_by_code = {r["code"]: r["amount"] * 6 for r in earnings + deductions + employer}
    employee = {
        "name": "Priya Raman", "code": "STEMP0001", "department": "Human Resources",
        "designation": "HR Senior Executive", "location": "Chennai HO", "date_of_joining": "01 Apr 2024",
        "pan": "XXXXXX234F", "uan": "100987654321", "bank_name": "HDFC Bank", "account": "XXXXXXXXXX1696",
        "ifsc": "HDFC0002406",
    }
    attendance = {"days_in_month": 30, "working_days": 25, "paid_days": "28", "lop_days": "2",
                  "present_days": "23", "paid_leave_days": "2"}
    leave = [{"code": "CL", "name": "Casual Leave", "credited": "6", "used": "2", "balance": "4"},
             {"code": "EL", "name": "Earned Leave", "credited": "12", "used": "3", "balance": "9"}]
    ytd_totals = {"gross": gross * 6, "deductions": total_ded * 6, "net": (gross - total_ded) * 6}
    return assemble(settings, logo_uri, date(2026, 9, 1), date(2026, 9, 30), date(2026, 9, 30), employee,
                    earnings, deductions, employer, gross, total_ded, gross - total_ded, attendance, leave,
                    ytd_by_code, ytd_totals, run_label="Sample")


def pdf_password(settings, employee: Optional[Employee]) -> Optional[str]:
    """PAN in capitals or date of birth as DDMMYYYY; None when off or the
    value is missing (the PDF is then left unprotected)."""
    if employee is None or settings.pdf_password == "NONE":
        return None
    if settings.pdf_password == "PAN" and employee.pan_number:
        return str(employee.pan_number).strip().upper()
    if settings.pdf_password == "DOB" and employee.date_of_birth:
        return employee.date_of_birth.strftime("%d%m%Y")
    return None
