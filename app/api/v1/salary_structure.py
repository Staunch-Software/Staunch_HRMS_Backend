from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.salary_component import SalaryComponent
from app.models.salary_structure import EmployeeSalaryStructureAssignment
from app.schemas.salary_structure import (
    SalaryStructureAssignmentCreate,
    SalaryStructureAssignmentRead,
    SalaryStructureAssignmentResolved,
)
from app.services.salary_structure_resolver import resolve_salary_structure

router = APIRouter(tags=["salary-structure"], dependencies=[Depends(get_current_user)])


@router.get(
    "/employees/{employee_id}/salary-structure",
    response_model=list[SalaryStructureAssignmentResolved],
)
def get_employee_salary_structure(employee_id: int, as_of: date | None = None, db: Session = Depends(get_db)):
    if not db.get(Employee, employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    as_of = as_of or date.today()
    rows = resolve_salary_structure(db, employee_id, as_of)

    result = []
    for row in rows:
        component = db.get(SalaryComponent, row.salary_component_id)
        result.append(
            SalaryStructureAssignmentResolved(
                id=row.id,
                employee_id=row.employee_id,
                salary_component_id=row.salary_component_id,
                value_override=row.value_override,
                effective_from=row.effective_from,
                effective_to=row.effective_to,
                component_code=component.code if component else "UNKNOWN",
                component_name=component.name if component else "Unknown component",
                component_type=component.component_type if component else "Unknown",
            )
        )
    return result


@router.post(
    "/employees/{employee_id}/salary-structure",
    response_model=SalaryStructureAssignmentRead,
    status_code=status.HTTP_201_CREATED,
)
def assign_salary_component(
    employee_id: int, payload: SalaryStructureAssignmentCreate, db: Session = Depends(get_db)
):
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    component = db.get(SalaryComponent, payload.salary_component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Salary component not found")

    # Close out any existing open row for the same component before inserting
    # the new one — history is never mutated in place.
    existing_open = (
        db.query(EmployeeSalaryStructureAssignment)
        .filter(
            EmployeeSalaryStructureAssignment.employee_id == employee_id,
            EmployeeSalaryStructureAssignment.salary_component_id == payload.salary_component_id,
            EmployeeSalaryStructureAssignment.effective_to.is_(None),
        )
        .first()
    )
    if existing_open:
        existing_open.effective_to = payload.effective_from
        db.add(existing_open)

    new_row = EmployeeSalaryStructureAssignment(
        employee_id=employee_id,
        salary_component_id=payload.salary_component_id,
        value_override=payload.value_override,
        effective_from=payload.effective_from,
        effective_to=None,
    )
    db.add(new_row)
    db.commit()
    db.refresh(new_row)
    return new_row


@router.patch(
    "/salary-structure-assignments/{assignment_id}/end",
    response_model=SalaryStructureAssignmentRead,
)
def end_salary_structure_assignment(assignment_id: int, db: Session = Depends(get_db)):
    """Ends an assignment as-of today, preserving history — never a hard
    delete, so past payroll runs stay reproducible against this row."""
    row = db.get(EmployeeSalaryStructureAssignment, assignment_id)
    if not row:
        raise HTTPException(status_code=404, detail="Salary structure assignment not found")
    if row.effective_to is None or row.effective_to > date.today():
        row.effective_to = date.today()
    db.commit()
    db.refresh(row)
    return row
