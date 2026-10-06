"""Resolves an employee's effective salary structure as-of a given date.
Shared by the `/employees/{id}/salary-structure` API (Section 1) and the
payroll engine (Section 6, step 2) so both use identical "latest non-expired
row per component" semantics.
"""
from datetime import date

from sqlalchemy.orm import Session

from app.models.salary_structure import EmployeeSalaryStructureAssignment


def resolve_salary_structure(
    db: Session, employee_id: int, as_of: date
) -> list[EmployeeSalaryStructureAssignment]:
    """Returns one row per salary_component_id: the assignment row that is
    open (effective_to is null) or not yet expired (effective_to >= as_of)
    and already effective (effective_from <= as_of) as of `as_of`. If more
    than one such row exists for a component (should not normally happen
    given the close-out-and-insert editing discipline), the one with the
    latest effective_from wins.
    """
    candidates = (
        db.query(EmployeeSalaryStructureAssignment)
        .filter(
            EmployeeSalaryStructureAssignment.employee_id == employee_id,
            EmployeeSalaryStructureAssignment.effective_from <= as_of,
            (EmployeeSalaryStructureAssignment.effective_to.is_(None))
            | (EmployeeSalaryStructureAssignment.effective_to >= as_of),
        )
        .order_by(EmployeeSalaryStructureAssignment.effective_from.desc())
        .all()
    )

    latest_by_component: dict[int, EmployeeSalaryStructureAssignment] = {}
    for row in candidates:
        if row.salary_component_id not in latest_by_component:
            latest_by_component[row.salary_component_id] = row
    return list(latest_by_component.values())
