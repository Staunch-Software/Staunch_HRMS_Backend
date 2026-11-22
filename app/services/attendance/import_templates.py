"""Pre-filled .xlsx templates for the three attendance uploads (same style as
app.services.excel_import.templates). The data sheet is always first; an
"Instructions" sheet follows."""
from datetime import date, timedelta
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.services.attendance.imports import (
    BIOMETRIC_GROUP_CODE,
    KIND_DAILY,
    KIND_MUSTER,
    month_profiles,
)
from app.services.attendance.overrides import DAY_STATUS_CODES, active_leave_codes

DAILY_HEADERS = ["Employee ID", "Employee Name", "Date", "In Time", "Out Time", "Reason"]
SITE_HEADERS = ["Employee ID", "Employee Name", "Days Present", "OT Days", "OT Hours", "LOP Days", "Pay Group"]
FILE_PREFIX = {KIND_DAILY: "daily_corrections", KIND_MUSTER: "muster"}


def _name(employee: Employee) -> str:
    return " ".join(p for p in (employee.first_name, employee.middle_name, employee.last_name) if p)


def _active_employees(db: Session) -> list[Employee]:
    return db.query(Employee).filter(Employee.status == "Active").order_by(Employee.employee_code).all()


def _bold_row(ws, row: int = 1) -> None:
    for cell in ws[row]:
        cell.font = Font(bold=True)


def _instructions(wb: Workbook, lines: list[str]) -> None:
    ws = wb.create_sheet("Instructions")
    ws.column_dimensions["A"].width = 110
    for line in lines:
        ws.append([line])


def _daily(wb: Workbook, month_start: date, month_end: date) -> None:
    ws = wb.active
    ws.title = "Daily corrections"
    ws.append(DAILY_HEADERS)
    _bold_row(ws)
    for col, width in zip("ABCDEF", (14, 28, 12, 10, 10, 40)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "A2"
    _instructions(wb, [
        f"Daily corrections for {month_start:%B %Y}: one row per employee-day to correct.",
        f"Date must be within {month_start:%d-%m-%Y} and {month_end:%d-%m-%Y} (DD-MM-YYYY or an Excel date).",
        "In Time / Out Time: HH:MM (24h) or H:MM AM/PM. One of them may be blank to keep the biometric value.",
        "Out Time must be after In Time; overnight shifts are not supported.",
        "Employee ID is matched first, then the name. Reason is optional but recommended.",
    ])


def _muster(db: Session, wb: Workbook, month_start: date, month_end: date) -> None:
    ws = wb.active
    ws.title = "Muster"
    days = [month_start + timedelta(days=i) for i in range(month_end.day)]
    ws.append(["Employee ID", "Employee Name", *[d.day for d in days]])
    ws.append([None, None, *[d.strftime("%a") for d in days]])
    _bold_row(ws)
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["B"].width = 28
    ws.freeze_panes = "C3"
    for employee in _active_employees(db):
        ws.append([employee.employee_code, _name(employee)])
    leave_codes = active_leave_codes(db)
    _instructions(wb, [
        f"Monthly muster for {month_start:%B %Y}: one row per employee, one column per day.",
        f"Day codes: {', '.join(DAY_STATUS_CODES)} (P present, A absent, HD half day, WO week off, H holiday, "
        "OD on duty = present).",
        "Leave: put the leave type code (e.g. CL). Half day with leave for the other half: HD/CL.",
        "Active leave codes: " + (", ".join(sorted(leave_codes)) if leave_codes else "(leave types not set up yet)"),
        "A blank cell leaves that day unchanged. Rows with no codes are ignored.",
        "The weekday row under the header is ignored on upload.",
    ])


def _site(db: Session, wb: Workbook, month_start: date, month_end: date) -> None:
    ws = wb.active
    ws.title = "Site days"
    ws.append(SITE_HEADERS)
    _bold_row(ws)
    for col, width in zip("ABCDEFG", (14, 28, 13, 10, 10, 10, 14)):
        ws.column_dimensions[col].width = width
    ws.freeze_panes = "C2"
    employees = _active_employees(db)
    profiles = month_profiles(db, [e.id for e in employees], month_start, month_end)
    site_rows = [
        (profiles[e.id][1].code, e) for e in employees
        if e.id in profiles and profiles[e.id][1].code != BIOMETRIC_GROUP_CODE
    ]
    for group_code, employee in sorted(site_rows, key=lambda item: (item[0], item[1].employee_code)):
        ws.append([employee.employee_code, _name(employee), None, None, None, None, group_code])
    _instructions(wb, [
        f"Site days present for {month_start:%B %Y} ({month_end.day} days).",
        "Days Present is required; OT Days, OT Hours and LOP Days are optional (blank keeps the current value).",
        "Values become the employee's monthly payroll inputs for the month. Pay Group is for reference only.",
        f"Employees in {BIOMETRIC_GROUP_CODE} take their days from biometric attendance and are not listed.",
    ])


def build_template(db: Session, kind: str, month_start: date, month_end: date) -> tuple[bytes, str]:
    wb = Workbook()
    if kind == KIND_DAILY:
        _daily(wb, month_start, month_end)
    elif kind == KIND_MUSTER:
        _muster(db, wb, month_start, month_end)
    else:
        _site(db, wb, month_start, month_end)
    buffer = BytesIO()
    wb.save(buffer)
    filename = f"attendance_{FILE_PREFIX.get(kind, 'site_days')}_{month_start:%Y-%m}.xlsx"
    return buffer.getvalue(), filename
