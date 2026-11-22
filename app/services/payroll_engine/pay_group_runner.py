"""Payroll-run integration for pay groups. A run with `pay_group_id` set is
scoped to the employees whose pay profile (effective on the run's
period_end) belongs to that group, and every line item is computed by the
group's strategy (FORMULA groups: their definition, whose version the run
records). Line items are persisted exactly like the component engine's, so
review -> approve -> bank file is unchanged.
"""
import calendar
from datetime import date

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.variable_pay import VariablePayInput
from app.services.payroll_engine.pay_groups import (
    PayGroupCalculationError,
    PayGroupParamsError,
    PayInputs,
    PayResult,
    get_strategy,
    group_config,
)
from app.services.payroll_engine.pay_groups.formula_strategy import FORMULA
from app.services.payroll_engine.statutory import tds as tds_calc


def resolve_pay_profile(db: Session, employee_id: int, as_of: date) -> EmployeePayProfile | None:
    """The profile in force on `as_of` (latest effective_from wins), same
    semantics as resolve_salary_structure."""
    return (
        db.query(EmployeePayProfile)
        .filter(
            EmployeePayProfile.employee_id == employee_id,
            EmployeePayProfile.effective_from <= as_of,
            (EmployeePayProfile.effective_to.is_(None)) | (EmployeePayProfile.effective_to >= as_of),
        )
        .order_by(EmployeePayProfile.effective_from.desc(), EmployeePayProfile.id.desc())
        .first()
    )


def _in_payable_status(employee: Employee, run: PayrollRun) -> bool:
    is_leaver_this_period = (
        employee.date_of_leaving is not None
        and run.period_start <= employee.date_of_leaving <= run.period_end
    )
    return employee.status == "Active" or is_leaver_this_period


def resolve_pay_group_scope(db: Session, run: PayrollRun) -> list[tuple[Employee, EmployeePayProfile]]:
    profiles = (
        db.query(EmployeePayProfile)
        .filter(
            EmployeePayProfile.pay_group_id == run.pay_group_id,
            EmployeePayProfile.effective_from <= run.period_end,
            (EmployeePayProfile.effective_to.is_(None)) | (EmployeePayProfile.effective_to >= run.period_end),
        )
        .all()
    )
    scope = []
    for employee_id in sorted({p.employee_id for p in profiles}):
        # An employee may hold an older and a newer row in the same group;
        # only the one actually in force counts, and it must be this group.
        profile = resolve_pay_profile(db, employee_id, run.period_end)
        if profile is None or profile.pay_group_id != run.pay_group_id:
            continue
        employee = db.get(Employee, employee_id)
        if employee is not None and _in_payable_status(employee, run):
            scope.append((employee, profile))
    return scope


def apply_tds(db: Session, run: PayrollRun, employee: Employee, result: PayResult, net_rounding: str) -> None:
    """Adds this month's TDS (on gross before net-pay round-off) as a 'TDS'
    deduction and re-rounds net pay; no line when it is 0."""
    tds = tds_calc.compute_monthly_tds(db, employee, run.period_end, result.gross_before_round_off(), run_id=run.id)
    if tds["warning"]:
        result.warnings.append(tds["warning"])
    if tds["amount"] > 0:
        basis = (
            f"{tds['regime']} regime FY {tds['financial_year']}: annual tax {tds['annual_tax']} "
            f"on projected {tds['annual_projection']}, less YTD TDS {tds['ytd_tds']}, "
            f"over {tds['months_remaining']} months"
        )
        result.add_deduction(tds_calc.TDS_CODE, "Income tax (TDS)", tds["amount"], basis, net_rounding)


def calculate_pay_group_line_items(db: Session, run: PayrollRun) -> list[PayrollRunLineItem]:
    from app.services.payroll_engine.engine import _employee_snapshot  # local: engine imports this module

    group = db.get(PayGroup, run.pay_group_id)
    if group is None:
        raise PayGroupParamsError(f"Pay group {run.pay_group_id} not found.")
    if not group.is_active:
        raise PayGroupParamsError(f"Pay group '{group.code}' is inactive.")
    strategy = get_strategy(group.calc_model)
    config = group_config(group)
    errors = strategy.validate_params(config)
    if errors:
        what = "definition" if group.calc_model == FORMULA else "params"
        raise PayGroupParamsError(f"Pay group '{group.code}' has an invalid {what}: " + "; ".join(errors))
    run.pay_group_definition_version = group.definition_version if group.calc_model == FORMULA else None
    days_in_month = calendar.monthrange(run.period_end.year, run.period_end.month)[1]
    net_rounding = config.get("net_rounding") or "NONE"
    tds_enabled = config.get("tds_enabled")

    header = {
        "component_code": "_PAY_GROUP",
        "component_name": "Pay group",
        "component_type": "Info",
        "kind": "info",
        "calculation_method": f"PayGroup:{group.calc_model}",
        "formula_or_value": group.code,
        "resolved_amount": 0.0,
    }
    if group.calc_model == FORMULA:
        header["definition_version"] = group.definition_version

    items = []
    for employee, profile in resolve_pay_group_scope(db, run):
        snapshot = {**_employee_snapshot(db, employee), "pay_group_code": group.code}
        vi = (
            db.query(VariablePayInput)
            .filter(
                VariablePayInput.employee_id == employee.id,
                VariablePayInput.period_start == run.period_start,
                VariablePayInput.period_end == run.period_end,
            )
            .first()
        )
        try:
            inputs = PayInputs.from_variable_input(vi, days_in_month)
            result = strategy.calculate(profile, employee, inputs, config)
            if tds_enabled is None or tds_enabled:
                apply_tds(db, run, employee, result, net_rounding)
        except PayGroupCalculationError as exc:
            items.append(PayrollRunLineItem(
                payroll_run_id=run.id, employee_id=employee.id, employee_snapshot=snapshot,
                gross_earnings=0, total_deductions=0, total_reimbursements=0, net_pay=0,
                status="Error", computation_breakdown=[header], error_message=str(exc),
            ))
            continue
        items.append(PayrollRunLineItem(
            payroll_run_id=run.id,
            employee_id=employee.id,
            employee_snapshot=snapshot,
            gross_earnings=float(result.gross),
            total_deductions=float(result.total_deductions),
            total_reimbursements=0,
            net_pay=float(result.net_pay),
            pf_wages=float(result.pf_wages) if result.pf_wages is not None else None,
            esi_wages=float(result.esi_wages) if result.esi_wages is not None else None,
            paid_days=float(result.paid_days) if result.paid_days is not None else None,
            lop_days=float(result.lop_days) if result.lop_days is not None else None,
            status="Included",
            computation_breakdown=[header, *result.breakdown(group.calc_model)],
            error_message=None,
        ))
    return items
