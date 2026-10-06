"""Generates pre-filled .xlsx templates for both upload modes. Pre-filling
Employee Code/Name/Pay Period (rather than shipping a blank template) is
deliberate — it cuts typos versus hand-typing employee codes.
"""
from io import BytesIO

from openpyxl import Workbook

from app.models.employee import Employee
from app.models.payroll_run import PayrollRun

VARIABLE_INPUTS_HEADERS = [
    "Employee Code",
    "Employee Name",
    "Pay Period",
    "Days Present",
    "LOP Days",
    "Overtime Hours",
    "Bonus Amount",
    "Bonus Reason",
    "Ad-hoc Deduction Amount",
    "Ad-hoc Deduction Reason",
    "Remarks",
]


def _employee_name(employee: Employee) -> str:
    return f"{employee.first_name} {employee.last_name or ''}".strip()


def build_variable_inputs_template(run: PayrollRun, employees: list[Employee]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Variable Inputs"
    ws.append(VARIABLE_INPUTS_HEADERS)

    pay_period = run.period_start.strftime("%Y-%m")
    for employee in employees:
        ws.append(
            [
                employee.employee_code,
                _employee_name(employee),
                pay_period,
                "",  # Days Present - left blank, required
                0,  # LOP Days
                0,  # Overtime Hours
                0,  # Bonus Amount
                "",  # Bonus Reason
                0,  # Ad-hoc Deduction Amount
                "",  # Ad-hoc Deduction Reason
                "",  # Remarks
            ]
        )

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def build_final_sheet_template(run: PayrollRun, employees: list[Employee], component_codes: list[str]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Final Payroll Sheet"
    headers = ["Employee Code", "Employee Name", "Pay Period", "Gross Earnings", "Total Deductions", "Net Pay"]
    headers.extend(component_codes)
    headers.append("Remarks")
    ws.append(headers)

    pay_period = run.period_start.strftime("%Y-%m")
    for employee in employees:
        row = [employee.employee_code, _employee_name(employee), pay_period, "", "", ""]
        row.extend(["" for _ in component_codes])
        row.append("")
        ws.append(row)

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
