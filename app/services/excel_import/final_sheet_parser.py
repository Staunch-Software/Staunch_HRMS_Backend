"""Parses + validates the Final Payroll Sheet Excel template. Commits from
this template bypass the calculation engine entirely — the sheet's own
figures are taken as authoritative (see payroll_run_uploads.py).
"""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.payroll_run import PayrollRun
from app.services.excel_import.common import (
    FINAL_SHEET_TOLERANCE,
    RowResult,
    clean_str,
    flag_duplicate_employee_codes,
    pay_period_str,
    to_number,
)
from app.services.excel_import.excel_reader import ExcelParseError
from app.services.payroll_engine.engine import _resolve_employee_scope  # noqa: WPS437 - shared scope logic, not duplicated

REQUIRED_COLUMNS = {"Employee Code", "Pay Period", "Net Pay"}


def validate_final_sheet_records(
    db: Session, run: PayrollRun, records: list[dict], component_codes: list[str]
) -> list[RowResult]:
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
    component_code_set = set(component_codes)

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

        gross, gross_err = to_number(row.get("Gross Earnings"), required=False, field_name="Gross Earnings")
        if gross_err:
            warnings.append(gross_err)
        elif gross is None:
            warnings.append("Gross Earnings was not provided — it will be inferred from Net Pay / Total Deductions.")

        deductions, ded_err = to_number(row.get("Total Deductions"), required=False, field_name="Total Deductions")
        if ded_err:
            warnings.append(ded_err)
        elif deductions is None:
            warnings.append("Total Deductions was not provided — it will be inferred from Net Pay / Gross Earnings.")

        net_pay, net_err = to_number(row.get("Net Pay"), required=True, field_name="Net Pay")
        if net_err:
            errors.append(net_err)

        if gross is not None and deductions is not None and net_pay is not None:
            expected_net = Decimal(str(gross)) - Decimal(str(deductions))
            actual_net = Decimal(str(net_pay))
            if abs(expected_net - actual_net) > FINAL_SHEET_TOLERANCE:
                errors.append(
                    f"Gross Earnings ({gross}) minus Total Deductions ({deductions}) = {expected_net}, "
                    f"which does not match Net Pay ({net_pay}) within the "
                    f"{FINAL_SHEET_TOLERANCE} rounding tolerance."
                )

        components: dict[str, float] = {}
        for code in component_code_set:
            if code not in row:
                continue
            raw_value = row.get(code)
            if raw_value is None:
                continue
            value, comp_err = to_number(raw_value, field_name=f"Component '{code}'")
            if comp_err:
                errors.append(comp_err)
            else:
                components[code] = value

        remarks = clean_str(row.get("Remarks"))

        data = {
            "employee_code": employee_code,
            "employee_id": employee.id if employee else None,
            "pay_period": pay_period,
            "gross_earnings": gross,
            "total_deductions": deductions,
            "net_pay": net_pay,
            "components": components,
            "remarks": remarks,
        }
        results.append(RowResult(row_number=idx, data=data, errors=errors, warnings=warnings))

    flag_duplicate_employee_codes(results)
    return results
