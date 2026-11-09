"""Payroll-run integration for pay groups. A run with `pay_group_id` set is
scoped to the employees whose pay profile (effective on the run's
period_end) belongs to that group, and every line item is computed by the
group's strategy. Line items are persisted exactly like the component
engine's, so review -> approve -> bank file is unchanged.
"""
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
    get_strategy,
)


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


def calculate_pay_group_line_items(db: Session, run: PayrollRun) -> list[PayrollRunLineItem]:
    from app.services.payroll_engine.engine import _employee_snapshot  # local: engine imports this module

    group = db.get(PayGroup, run.pay_group_id)
    if group is None:
        raise PayGroupParamsError(f"Pay group {run.pay_group_id} not found.")
    if not group.is_active:
        raise PayGroupParamsError(f"Pay group '{group.code}' is inactive.")
    strategy = get_strategy(group.calc_model)
    errors = strategy.validate_params(group.params)
    if errors:
        raise PayGroupParamsError(f"Pay group '{group.code}' has invalid params: " + "; ".join(errors))

    header = {
        "component_code": "_PAY_GROUP",
        "component_name": "Pay group",
        "component_type": "Info",
        "kind": "info",
        "calculation_method": f"PayGroup:{group.calc_model}",
        "formula_or_value": group.code,
        "resolved_amount": 0.0,
    }

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
            result = strategy.calculate(profile, employee, PayInputs.from_variable_input(vi), group.params)
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
