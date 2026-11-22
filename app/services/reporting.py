"""Reporting-manager helpers shared by the employee API, the master import
and the self-service endpoints."""
from typing import Optional

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.org import Department


def employee_full_name(employee: Optional[Employee]) -> Optional[str]:
    if employee is None:
        return None
    return " ".join(p for p in (employee.first_name, employee.middle_name, employee.last_name) if p) or None


def has_direct_reports(db: Session, employee_id: int, active_only: bool = True) -> bool:
    query = db.query(Employee.id).filter(Employee.reporting_manager_id == employee_id)
    if active_only:
        query = query.filter(Employee.status == "Active")
    return query.first() is not None


def managed_department_ids(db: Session, employee_id: Optional[int]) -> list[int]:
    if employee_id is None:
        return []
    rows = db.query(Department.id).filter(Department.manager_employee_id == employee_id)
    return [row[0] for row in rows.order_by(Department.id)]


def department_member_ids(db: Session, manager_employee_id: Optional[int]) -> list[int]:
    """Employees (other than the manager) in the departments they manage:
    whose leave, permission and WFH requests they approve."""
    department_ids = managed_department_ids(db, manager_employee_id)
    if not department_ids:
        return []
    rows = db.query(Employee.id).filter(
        Employee.department_id.in_(department_ids), Employee.id != manager_employee_id
    )
    return [row[0] for row in rows.order_by(Employee.id)]


def has_team(db: Session, employee_id: int) -> bool:
    """Manages at least one department, or still has direct reports."""
    return bool(managed_department_ids(db, employee_id)) or has_direct_reports(db, employee_id)


def chain_reaches(manager_of: dict, start: Optional[object], target: object) -> bool:
    """True if following manager_of links from `start` reaches `target`."""
    seen = set()
    current = start
    while current is not None and current not in seen:
        if current == target:
            return True
        seen.add(current)
        current = manager_of.get(current)
    return False


def reporting_manager_error(db: Session, employee_id: Optional[int], manager_id: Optional[int]) -> Optional[str]:
    """Validation message for setting `manager_id` as the reporting manager
    of `employee_id` (None for a new employee), or None if it is allowed."""
    if manager_id is None:
        return None
    manager = db.get(Employee, manager_id)
    if manager is None:
        return "Reporting manager not found."
    if employee_id is None:
        return None
    if manager_id == employee_id:
        return "An employee cannot be their own reporting manager."
    seen = set()
    current = manager
    while current is not None and current.id not in seen:
        if current.reporting_manager_id == employee_id:
            return (
                f"{employee_full_name(manager) or 'That employee'} already reports (directly or indirectly) "
                "to this employee, so they cannot be their reporting manager."
            )
        seen.add(current.id)
        current = db.get(Employee, current.reporting_manager_id) if current.reporting_manager_id else None
    return None
