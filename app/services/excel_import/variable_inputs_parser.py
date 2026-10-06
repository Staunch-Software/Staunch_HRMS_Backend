"""Parses + validates the Variable Inputs Excel template. Server-side only
so validation can check against live DB state (active employees in the
run's scope, the run's own period) that a client-side parser wouldn't have.
"""
from sqlalchemy.orm import Session

from app.models.payroll_run import PayrollRun
from app.services.excel_import.common import (
    RowResult,
    clean_str,
    flag_duplicate_employee_codes,
    pay_period_str,
    to_number,
)
from app.services.excel_import.excel_reader import ExcelParseError
from app.services.payroll_engine.engine import _resolve_employee_scope  # noqa: WPS437 - shared scope logic, not duplicated

REQUIRED_COLUMNS = {"Employee Code", "Pay Period", "Days Present"}

# Fixed pseudo component-codes for the two ad-hoc columns the template
# offers (Bonus, Ad-hoc Deduction) — the template doesn't let the uploader
# pick an arbitrary component code per row, unlike the manual entry form.
# For either to actually affect a payslip, the employee must have a salary
# structure component with a matching code and
# calculation_method="Based on Attendance/Input" (same mechanism the manual
# VariableInputsForm.js already relies on).
BONUS_COMPONENT_CODE = "BONUS"
ADHOC_DEDUCTION_COMPONENT_CODE = "ADHOC_DEDUCTION"


def validate_variable_inputs_records(db: Session, run: PayrollRun, records: list[dict]) -> list[RowResult]:
    """Validates already-parsed rows. Split out from parsing so the commit
    endpoint can defensively re-validate the same rows against current DB
    state without needing to re-read the original file bytes.
    """
    if not records:
        raise ExcelParseError("The uploaded workbook has no data rows.")

    missing_columns = REQUIRED_COLUMNS - set(records[0].keys())
    if missing_columns:
        raise ExcelParseError(f"Missing required column(s): {', '.join(sorted(missing_columns))}")

    employees_by_code = {emp.employee_code: emp for emp in _resolve_employee_scope(db, run)}
    run_period = run.period_start.strftime("%Y-%m")

    results: list[RowResult] = []
    for idx, row in enumerate(records, start=2):  # row 1 is the header
        errors: list[str] = []
        warnings: list[str] = []

        employee_code = clean_str(row.get("Employee Code"))
        employee = None
        if not employee_code:
            errors.append("Employee Code is required.")
        else:
            employee = employees_by_code.get(employee_code)
            if employee is None:
                errors.append(
                    f"Employee Code '{employee_code}' was not found among active employees in this run's scope."
                )

        pay_period = pay_period_str(row.get("Pay Period"))
        if not pay_period:
            errors.append("Pay Period is required.")
        elif pay_period != run_period:
            errors.append(f"Pay Period '{pay_period}' does not match this run's period ({run_period}).")

        days_present, err = to_number(row.get("Days Present"), required=True, field_name="Days Present")
        if err:
            errors.append(err)

        lop_days, err = to_number(row.get("LOP Days"), default=0, field_name="LOP Days")
        if err:
            errors.append(err)

        overtime_hours, err = to_number(row.get("Overtime Hours"), default=0, field_name="Overtime Hours")
        if err:
            errors.append(err)

        bonus_amount, err = to_number(row.get("Bonus Amount"), default=0, field_name="Bonus Amount")
        if err:
            errors.append(err)
        bonus_reason = clean_str(row.get("Bonus Reason"))
        if (bonus_amount or 0) > 0 and not bonus_reason:
            errors.append("Bonus Reason is required when Bonus Amount > 0.")

        adhoc_deduction_amount, err = to_number(
            row.get("Ad-hoc Deduction Amount"), default=0, field_name="Ad-hoc Deduction Amount"
        )
        if err:
            errors.append(err)
        adhoc_deduction_reason = clean_str(row.get("Ad-hoc Deduction Reason"))
        if (adhoc_deduction_amount or 0) > 0 and not adhoc_deduction_reason:
            errors.append("Ad-hoc Deduction Reason is required when Ad-hoc Deduction Amount > 0.")

        remarks = clean_str(row.get("Remarks"))

        data = {
            "employee_code": employee_code,
            "employee_id": employee.id if employee else None,
            "pay_period": pay_period,
            "days_present": days_present,
            "lop_days": lop_days or 0,
            "overtime_hours": overtime_hours or 0,
            "bonus_amount": bonus_amount or 0,
            "bonus_reason": bonus_reason,
            "adhoc_deduction_amount": adhoc_deduction_amount or 0,
            "adhoc_deduction_reason": adhoc_deduction_reason,
            "remarks": remarks,
        }
        results.append(RowResult(row_number=idx, data=data, errors=errors, warnings=warnings))

    flag_duplicate_employee_codes(results)
    return results
