"""POST /pay-groups/{id}/test: one calculation with a line-by-line trace,
on the saved or an unsaved definition, for an employee's pay profile and/or
profile values typed in the calculator."""
import calendar
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.variable_pay import VariablePayInput
from app.services.payroll_engine.formula import catalog as cat
from app.services.payroll_engine.formula.calculator import calculate, jsonable
from app.services.payroll_engine.formula.compiled import build, validate_definition
from app.services.payroll_engine.formula.errors import FormulaRuntimeError
from app.services.payroll_engine.pay_groups.base import PayGroupCalculationError, PayInputs, PayProfileData, dec

PROFILE_KEYS = ("monthly_salary", "daily_wage", "skill_level", "pf_applicable", "esi_applicable", "pf_cap",
                "fixed_monthly_amount")
INPUT_FIELDS = {"DAYS_PRESENT": "present_days", "LOP_DAYS": "lop_days", "OT_HOURS": "overtime_hours",
                "OT_DAYS": "overtime_days", "PAID_LEAVE_DAYS": "paid_leave_days"}


class TestInputError(ValueError):
    pass


def month_bounds(month: Optional[str]) -> tuple[date, date]:
    if not month:
        today = date.today()
        month = f"{today.year:04d}-{today.month:02d}"
    try:
        year, mon = (int(x) for x in month.split("-"))
        first = date(year, mon, 1)
    except (ValueError, TypeError):
        raise TestInputError("month must be YYYY-MM, e.g. 2026-09") from None
    return first, date(year, mon, calendar.monthrange(year, mon)[1])


def build_profile(stored, overrides: Optional[dict]) -> PayProfileData:
    profile = PayProfileData(**{k: getattr(stored, k, None) for k in PROFILE_KEYS},
                             extra=dict(getattr(stored, "extra", None) or {}))
    for key, value in (overrides or {}).items():
        if key == "extra":
            profile.extra = {**profile.extra, **(value or {})}
        elif key in PROFILE_KEYS:
            setattr(profile, key, value)
        else:
            raise TestInputError(f"unknown profile field '{key}' (allowed: {', '.join(PROFILE_KEYS)}, extra)")
    return profile


def build_inputs(raw: Optional[dict], vi, days_in_month: int) -> tuple[PayInputs, str]:
    if raw is None:
        if vi is not None:
            return PayInputs.from_variable_input(vi, days_in_month), "monthly_input"
        return PayInputs(has_input_row=False, days_in_month=days_in_month), "none"
    inputs = PayInputs(days_in_month=days_in_month)
    for key, value in raw.items():
        code = str(key).strip().upper()
        if code == "HAS_INPUT":
            inputs.has_input_row = bool(value)
            continue
        if value is None or value == "":
            continue
        try:
            amount = Decimal(str(value))
        except Exception:  # noqa: BLE001 - any non-number
            raise TestInputError(f"input {code} must be a number") from None
        if code in INPUT_FIELDS:
            setattr(inputs, INPUT_FIELDS[code], amount)
        elif code in cat.INPUT_EARNING_CODES:
            inputs.earnings[code] = amount
        elif code in cat.INPUT_DEDUCTION_CODES:
            inputs.deductions[code] = amount
        else:
            allowed = ", ".join([*INPUT_FIELDS, *cat.INPUT_CODES, "HAS_INPUT"])
            raise TestInputError(f"unknown input '{code}' (allowed: {allowed})")
    return inputs, "body"


def _inputs_json(inputs: PayInputs, source: str) -> dict:
    out = {name: jsonable(getattr(inputs, attr)) for name, attr in INPUT_FIELDS.items()}
    return {**out, **{k: jsonable(v) for k, v in {**inputs.earnings, **inputs.deductions}.items()},
            "HAS_INPUT": inputs.has_input_row, "source": source}


def run_test(db: Session, definition: Any, employee, stored_profile, profile_overrides: Optional[dict],
             raw_inputs: Optional[dict], month: Optional[str], include_tds: bool = True) -> dict:
    first, last = month_bounds(month)
    validation = validate_definition(definition)
    out: dict[str, Any] = {
        "ok": False, "error": None, "month": first.strftime("%Y-%m"), "days_in_month": last.day,
        "validation": {"errors": validation.errors, "warnings": validation.warnings},
        "employee": ({"id": employee.id, "employee_code": employee.employee_code,
                      "name": " ".join(filter(None, [employee.first_name, getattr(employee, "last_name", None)]))}
                     if employee is not None else None),
        "variables": {}, "lines": [], "totals": None, "line_items": [], "warnings": [],
    }
    if not validation.ok:
        out["error"] = {"message": "The definition has errors; fix them before testing.", "line": None,
                        "field": None, "position": None}
        return out
    profile = build_profile(stored_profile, profile_overrides)
    vi = None
    if employee is not None and raw_inputs is None:
        vi = db.query(VariablePayInput).filter(
            VariablePayInput.employee_id == employee.id, VariablePayInput.period_start == first,
            VariablePayInput.period_end == last).first()
    inputs, source = build_inputs(raw_inputs, vi, last.day)
    numeric = ("monthly_salary", "daily_wage", "pf_cap", "fixed_monthly_amount")
    out["profile"] = {k: jsonable(dec(v)) if k in numeric and v not in (None, "") else v
                      for k, v in vars(profile).items()}
    out["inputs"] = _inputs_json(inputs, source)
    compiled = build(validation)
    try:
        result, trace = calculate(compiled, profile, employee, inputs, trace=True)
    except PayGroupCalculationError as exc:
        trace = getattr(exc, "trace", None) or {}
        out.update(variables=trace.get("variables") or {}, lines=trace.get("lines") or [])
        out["error"] = {"message": str(exc), "line": getattr(exc, "line", None),
                        "field": getattr(exc, "field", None), "position": getattr(exc, "position", None)}
        if not isinstance(exc, FormulaRuntimeError):
            out["error"]["line"] = None
        return out
    if include_tds and employee is not None and compiled.settings["tds_enabled"]:
        from app.services.payroll_engine.pay_group_runner import apply_tds

        class _Run:
            id, period_end = None, last
        apply_tds(db, _Run(), employee, result, compiled.settings["net_rounding"])
    out.update(ok=True, variables=trace["variables"], lines=trace["lines"], warnings=result.warnings,
               line_items=result.breakdown("FORMULA"))
    out["totals"] = {k: jsonable(getattr(result, k)) for k in (
        "gross", "total_deductions", "net_pay", "employer_cost", "paid_days", "lop_days", "pf_wages", "esi_wages")}
    return out
