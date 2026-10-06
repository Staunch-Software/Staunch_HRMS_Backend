"""The payroll calculation engine. `calculate_payroll_run` is the single
orchestrating entry point called by POST /payroll-runs/{id}/calculate. It
mutates `run` in place (status/totals/error_message) and creates/replaces
that run's PayrollRunLineItem rows. All money math uses decimal.Decimal.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.employee import Employee, EmployeeBankAccount
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.salary_component import SalaryComponent
from app.models.variable_pay import VariablePayInput
from app.services.formula.dependency_graph import build_dependency_graph, topological_sort
from app.services.formula.evaluator import FormulaEvaluationError, evaluate_formula
from app.services.payroll_engine import proration, rounding
from app.services.payroll_engine.statutory import esi as esi_calc
from app.services.payroll_engine.statutory import pf as pf_calc
from app.services.payroll_engine.statutory import pt as pt_calc
from app.services.payroll_engine.statutory import tds as tds_calc
from app.services.salary_structure_resolver import resolve_salary_structure

# statutory_type values whose amount is derived from the versioned config
# tables (pf_configs/esi_configs/pt_slabs) rather than the component's own
# `value`/calculation_method — UNLESS the component explicitly overrides with
# "Fixed Amount" (a deliberate manual override, matching the spec's example).
STATUTORY_TYPES_WITH_DEDICATED_CALC = {"PF", "ESI", "PT", "TDS"}


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def _financial_year_for(period_end: date) -> str:
    year = period_end.year
    if period_end.month >= 4:
        return f"{year}-{str(year + 1)[-2:]}"
    return f"{year - 1}-{str(year)[-2:]}"


def _resolve_employee_scope(db: Session, run: PayrollRun) -> list[Employee]:
    schedule = run.pay_schedule
    applies_to = schedule.applies_to if schedule else None
    departments = {str(x) for x in (applies_to or {}).get("departments", []) or []}
    locations = {str(x) for x in (applies_to or {}).get("locations", []) or []}

    scope: list[Employee] = []
    for employee in db.query(Employee).order_by(Employee.id).all():
        is_leaver_this_period = (
            employee.date_of_leaving is not None
            and run.period_start <= employee.date_of_leaving <= run.period_end
        )
        if employee.status != "Active" and not is_leaver_this_period:
            continue

        if applies_to and (departments or locations):
            dept_match = departments and employee.department_id is not None and str(employee.department_id) in departments
            loc_match = locations and employee.work_location_id is not None and str(employee.work_location_id) in locations
            if not (dept_match or loc_match):
                continue

        scope.append(employee)
    return scope


def _get_variable_input(db: Session, employee_id: int, run: PayrollRun) -> tuple[VariablePayInput | None, list[dict]]:
    warnings: list[dict] = []
    vi = (
        db.query(VariablePayInput)
        .filter(
            VariablePayInput.employee_id == employee_id,
            VariablePayInput.period_start == run.period_start,
            VariablePayInput.period_end == run.period_end,
        )
        .first()
    )
    if vi is None:
        warnings.append(_note(
            "No variable input found for this period — defaulted to full-period "
            "attendance (0 LOP). Enter variable inputs before calculating for accurate results."
        ))
    return vi, warnings


def _note(message: str) -> dict:
    return {
        "component_code": "_SYSTEM_NOTE",
        "component_name": "System Note",
        "component_type": "Info",
        "calculation_method": None,
        "formula_or_value": None,
        "resolved_amount": 0.0,
        "warning": message,
    }


def _ad_hoc_amount(items: list, code: str) -> Decimal:
    for item in items or []:
        if item.get("component_code") == code:
            return _to_decimal(item.get("amount"))
    return Decimal("0")


def _resolve_value_source(component: SalaryComponent, override: float | None) -> str | None:
    """The raw `value` string/number to use for Fixed Amount / Percentage
    formulas: the employee-level override if set, else the component's own
    stored value."""
    if override is not None:
        return str(override)
    return component.value


def calculate_payroll_run(db: Session, run: PayrollRun) -> None:
    run.status = "Processing"
    db.commit()

    try:
        _run_calculation(db, run)
        run.status = "Calculated"
        run.calculated_at = datetime.now(timezone.utc)
        run.error_message = None
        db.commit()
    except Exception as exc:  # noqa: BLE001 - any engine failure must land on the run, not crash the request
        db.rollback()
        run.status = "Failed"
        run.error_message = str(exc)
        db.commit()
        raise


def _run_calculation(db: Session, run: PayrollRun) -> None:
    # Replace any previous line items (e.g. a re-calculate) rather than
    # accumulating duplicates.
    db.query(PayrollRunLineItem).filter(PayrollRunLineItem.payroll_run_id == run.id).delete()
    db.flush()

    employees = _resolve_employee_scope(db, run)
    financial_year = _financial_year_for(run.period_end)

    total_gross = Decimal("0")
    total_deductions = Decimal("0")
    total_net = Decimal("0")

    for employee in employees:
        line_item = _calculate_employee(db, run, employee, financial_year)
        db.add(line_item)
        if line_item.status != "Error":
            total_gross += _to_decimal(line_item.gross_earnings)
            total_deductions += _to_decimal(line_item.total_deductions)
            total_net += _to_decimal(line_item.net_pay)

    run.total_gross = float(total_gross)
    run.total_deductions = float(total_deductions)
    run.total_net = float(total_net)
    run.employee_count = len(employees)


def _employee_snapshot(db: Session, employee: Employee) -> dict:
    bank = (
        db.query(EmployeeBankAccount)
        .filter(EmployeeBankAccount.employee_id == employee.id, EmployeeBankAccount.is_active.is_(True))
        .order_by(EmployeeBankAccount.id.desc())
        .first()
    )
    return {
        "employee_code": employee.employee_code,
        "name": f"{employee.first_name} {employee.last_name or ''}".strip(),
        "status": employee.status,
        "department": employee.department.name if employee.department else None,
        "designation": employee.designation.name if employee.designation else None,
        "work_location": employee.work_location.name if employee.work_location else None,
        "state_code": employee.work_location.state_code if employee.work_location else None,
        "bank_name": bank.bank_name if bank else None,
        "account_holder_name": bank.account_holder_name if bank else None,
        "account_number": bank.account_number if bank else None,
        "ifsc_code": bank.ifsc_code if bank else None,
    }


def _calculate_employee(db: Session, run: PayrollRun, employee: Employee, financial_year: str) -> PayrollRunLineItem:
    breakdown: list[dict] = []
    snapshot = _employee_snapshot(db, employee)

    try:
        structure_rows = resolve_salary_structure(db, employee.id, run.period_end)
        if not structure_rows:
            return PayrollRunLineItem(
                payroll_run_id=run.id,
                employee_id=employee.id,
                employee_snapshot=snapshot,
                gross_earnings=0,
                total_deductions=0,
                total_reimbursements=0,
                net_pay=0,
                status="Excluded",
                computation_breakdown=[_note("No salary structure assigned — employee excluded from this run.")],
                error_message=None,
            )

        component_by_id = {
            c.id: c for c in db.query(SalaryComponent).filter(
                SalaryComponent.id.in_([r.salary_component_id for r in structure_rows])
            ).all()
        }

        variable_input, vi_warnings = _get_variable_input(db, employee.id, run)
        breakdown.extend(vi_warnings)

        lop_days = _to_decimal(variable_input.lop_days) if variable_input else Decimal("0")
        factor, total_period_days, eligible_days = proration.compute_proration_factor(
            run.period_start, run.period_end, employee.date_of_joining, employee.date_of_leaving, lop_days
        )
        present_days = (
            _to_decimal(variable_input.present_days)
            if variable_input and variable_input.present_days is not None
            else Decimal(total_period_days) - lop_days
        )

        ad_hoc_earnings = variable_input.ad_hoc_earnings if variable_input else []
        ad_hoc_deductions = variable_input.ad_hoc_deductions if variable_input else []

        entries = []  # (component, override) for active, status=True components only
        for row in structure_rows:
            component = component_by_id.get(row.salary_component_id)
            if component is None or not component.status:
                continue
            entries.append((component, row.value_override))

        # --- Phase 1: earnings, in dependency order -------------------------
        earning_entries = [(c, o) for c, o in entries if c.component_type == "Earning"]
        earning_dicts = [
            {"code": c.code, "calculation_method": c.calculation_method, "value": _resolve_value_source(c, o)}
            for c, o in earning_entries
        ]
        earning_graph = build_dependency_graph(earning_dicts)
        earning_order = topological_sort(earning_graph)
        earning_by_code = {c.code: (c, o) for c, o in earning_entries}

        namespace: dict[str, float] = {}
        pf_wages = Decimal("0")
        esi_wages = Decimal("0")
        gross_earnings = Decimal("0")

        for code in earning_order:
            component, override = earning_by_code[code]
            value_source = _resolve_value_source(component, override)
            amount = Decimal("0")

            if component.calculation_method == "Fixed Amount":
                base = _to_decimal(value_source)
                amount = base * factor if component.is_prorated else base
            elif component.calculation_method == "Percentage/Formula":
                if not value_source:
                    breakdown.append(_component_entry(component, value_source, 0, warning="Formula is empty — resolved to 0."))
                    namespace[code] = 0.0
                    continue
                try:
                    result = evaluate_formula(value_source, namespace)
                except FormulaEvaluationError as exc:
                    breakdown.append(_component_entry(component, value_source, 0, warning=str(exc)))
                    namespace[code] = 0.0
                    continue
                amount = _to_decimal(result)
            elif component.calculation_method == "Based on Attendance/Input":
                amount = _ad_hoc_amount(ad_hoc_earnings, code)
            else:
                amount = Decimal("0")

            amount = rounding.apply_rounding(amount, component.rounding_rule).quantize(Decimal("0.01"))
            namespace[code] = float(amount)
            gross_earnings += amount
            if component.affects_pf:
                pf_wages += amount
            if component.affects_esi:
                esi_wages += amount
            breakdown.append(_component_entry(component, value_source, amount))

        namespace["PF_WAGES"] = float(pf_wages)
        namespace["ESI_WAGES"] = float(esi_wages)
        namespace["GROSS_EARNINGS"] = float(gross_earnings)

        # --- Phase 2: deductions + reimbursements, in dependency order ------
        other_entries = [(c, o) for c, o in entries if c.component_type in ("Deduction", "Reimbursement")]
        other_dicts = [
            {"code": c.code, "calculation_method": c.calculation_method, "value": _resolve_value_source(c, o)}
            for c, o in other_entries
        ]
        other_graph = build_dependency_graph(other_dicts)
        other_order = topological_sort(other_graph)
        other_by_code = {c.code: (c, o) for c, o in other_entries}

        total_deductions = Decimal("0")
        total_reimbursements = Decimal("0")

        for code in other_order:
            component, override = other_by_code[code]
            value_source = _resolve_value_source(component, override)
            amount = Decimal("0")
            warning = None

            if component.component_type == "Reimbursement":
                amount = _ad_hoc_amount(ad_hoc_earnings, code)
                if component.claim_limit_amount is not None:
                    cap = _to_decimal(component.claim_limit_amount)
                    if amount > cap:
                        warning = f"Claimed amount capped at claim_limit_amount ({cap})."
                        amount = cap
                amount = amount.quantize(Decimal("0.01"))
                namespace[code] = float(amount)
                total_reimbursements += amount
                breakdown.append(_component_entry(component, value_source, amount, warning=warning))
                continue

            # Deduction
            use_dedicated = (
                component.statutory_type in STATUTORY_TYPES_WITH_DEDICATED_CALC
                and component.calculation_method != "Fixed Amount"
            )
            if use_dedicated and component.statutory_type == "PF":
                config = pf_calc.get_active_pf_config(db, run.period_end)
                if config is None:
                    warning = "No active PF config found — PF not calculated (0)."
                    amount = Decimal("0")
                else:
                    result = pf_calc.calculate_pf(pf_wages, config)
                    amount = result["employee_pf"]
            elif use_dedicated and component.statutory_type == "ESI":
                config = esi_calc.get_active_esi_config(db, run.period_end)
                if config is None:
                    warning = "No active ESI config found — ESI not calculated (0)."
                    amount = Decimal("0")
                else:
                    result = esi_calc.calculate_esi(esi_wages, config)
                    amount = result["employee_esi"]
                    if not result["eligible"]:
                        warning = "Not ESI-eligible this run (ESI_WAGES above threshold)."
            elif use_dedicated and component.statutory_type == "PT":
                state_code = employee.work_location.state_code if employee.work_location else None
                result = pt_calc.calculate_pt(db, state_code, gross_earnings, run.period_end)
                amount = result["amount"]
                warning = result["warning"]
            elif use_dedicated and component.statutory_type == "TDS":
                result = tds_calc.estimate_monthly_tds(db, employee.id, gross_earnings * Decimal("12"), financial_year)
                amount = result["monthly_tds"]
                warning = result["note"]
            elif component.calculation_method == "Fixed Amount":
                amount = _to_decimal(value_source)
            elif component.calculation_method == "Percentage/Formula":
                if not value_source:
                    warning = "Formula is empty — resolved to 0."
                else:
                    try:
                        amount = _to_decimal(evaluate_formula(value_source, namespace))
                    except FormulaEvaluationError as exc:
                        warning = str(exc)
            elif component.calculation_method == "Based on Attendance/Input":
                amount = _ad_hoc_amount(ad_hoc_deductions, code)
            else:
                amount = Decimal("0")

            amount = rounding.apply_rounding(amount, component.rounding_rule).quantize(Decimal("0.01"))
            namespace[code] = float(amount)
            total_deductions += amount
            breakdown.append(_component_entry(component, value_source, amount, warning=warning))

        net_pay = (gross_earnings + total_reimbursements - total_deductions).quantize(Decimal("0.01"))

        return PayrollRunLineItem(
            payroll_run_id=run.id,
            employee_id=employee.id,
            employee_snapshot=snapshot,
            gross_earnings=float(gross_earnings),
            total_deductions=float(total_deductions),
            total_reimbursements=float(total_reimbursements),
            net_pay=float(net_pay),
            pf_wages=float(pf_wages),
            esi_wages=float(esi_wages),
            paid_days=float(present_days),
            lop_days=float(lop_days),
            status="Included",
            computation_breakdown=breakdown,
            error_message=None,
        )
    except ValueError as exc:
        # e.g. a circular formula dependency raised by topological_sort
        return PayrollRunLineItem(
            payroll_run_id=run.id,
            employee_id=employee.id,
            employee_snapshot=snapshot,
            gross_earnings=0,
            total_deductions=0,
            total_reimbursements=0,
            net_pay=0,
            status="Error",
            computation_breakdown=breakdown,
            error_message=str(exc),
        )


def _component_entry(component: SalaryComponent, formula_or_value, resolved_amount, warning: str | None = None) -> dict:
    entry = {
        "component_code": component.code,
        "component_name": component.name,
        "component_type": component.component_type,
        "calculation_method": component.calculation_method,
        "formula_or_value": formula_or_value,
        "resolved_amount": float(resolved_amount),
    }
    if warning:
        entry["warning"] = warning
    return entry
