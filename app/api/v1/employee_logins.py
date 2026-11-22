"""HR management of employee self-service logins. Temporary passwords are
returned once in the create/reset response and only their hash is stored."""
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import generate_temporary_password, hash_password, require_hr
from app.models.employee import Employee
from app.models.user import ROLE_EMPLOYEE, AdminUser
from app.schemas.employee_login import (
    BulkCreateLoginsRequest,
    BulkCreateLoginsResponse,
    EmployeeLoginRow,
    SkippedLogin,
    TemporaryCredential,
)
from app.services.leave import workflow
from app.services.reporting import employee_full_name

router = APIRouter(prefix="/employee-logins", tags=["employee-logins"], dependencies=[Depends(require_hr)])

HASH_WORKERS = 8


def _is_active(employee: Employee) -> bool:
    return (employee.status or "").strip().lower() == "active"


def _row(employee: Employee, user: Optional[AdminUser]) -> EmployeeLoginRow:
    return EmployeeLoginRow(
        employee_id=employee.id,
        employee_code=employee.employee_code,
        name=employee_full_name(employee),
        department=employee.department.name if employee.department else None,
        designation=employee.designation.name if employee.designation else None,
        employee_status=employee.status,
        has_login=user is not None,
        user_id=user.id if user else None,
        username=user.username if user else None,
        role=user.role if user else None,
        login_enabled=user.is_active if user else None,
        must_change_password=user.must_change_password if user else None,
        last_login_at=user.last_login_at if user else None,
    )


def _get_employee_and_login(db: Session, employee_id: int) -> tuple[Employee, AdminUser]:
    employee = db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    user = db.query(AdminUser).filter(AdminUser.employee_id == employee_id).first()
    if user is None:
        raise HTTPException(status_code=404, detail="This employee has no login yet")
    return employee, user


def _hash_all(passwords: list[str]) -> list[str]:
    if len(passwords) <= 1:
        return [hash_password(p) for p in passwords]
    with ThreadPoolExecutor(max_workers=HASH_WORKERS) as pool:
        return list(pool.map(hash_password, passwords))


@router.get("", response_model=list[EmployeeLoginRow])
def list_employee_logins(include_inactive: bool = False, db: Session = Depends(get_db)):
    users_by_employee = {
        u.employee_id: u for u in db.query(AdminUser).filter(AdminUser.employee_id.isnot(None)).all()
    }
    rows = []
    for employee in db.query(Employee).order_by(Employee.employee_code).all():
        user = users_by_employee.get(employee.id)
        if include_inactive or _is_active(employee) or user is not None:
            rows.append(_row(employee, user))
    return rows


@router.post("/bulk-create", response_model=BulkCreateLoginsResponse)
def bulk_create_logins(payload: BulkCreateLoginsRequest, db: Session = Depends(get_db)):
    missing: list[int] = []
    if payload.all_active:
        employees = db.query(Employee).filter(Employee.status == "Active").order_by(Employee.employee_code).all()
    elif payload.employee_ids:
        found = {e.id: e for e in db.query(Employee).filter(Employee.id.in_(payload.employee_ids)).all()}
        employees = [found[i] for i in dict.fromkeys(payload.employee_ids) if i in found]
        missing = [i for i in dict.fromkeys(payload.employee_ids) if i not in found]
    else:
        raise HTTPException(status_code=400, detail="Select at least one employee, or choose all active employees.")

    skipped = [SkippedLogin(employee_id=i, reason="Employee not found") for i in missing]
    linked_ids = {
        row[0] for row in db.query(AdminUser.employee_id).filter(AdminUser.employee_id.isnot(None)).all()
    }
    users_by_name = {u.username: u for u in db.query(AdminUser).all()}

    to_create: list[tuple[Employee, str, Optional[AdminUser]]] = []
    for employee in employees:
        name = employee_full_name(employee)
        username = (employee.employee_code or "").strip()
        reason = None
        orphan = None
        if employee.id in linked_ids:
            reason = "Already has a login"
        elif not _is_active(employee):
            reason = "Employee is not Active"
        elif not username:
            reason = "Employee has no employee code"
        elif username in users_by_name:
            existing = users_by_name[username]
            if existing.role == ROLE_EMPLOYEE and existing.employee_id is None:
                orphan = existing
            else:
                reason = f"Username '{username}' is already used by another login"
        if reason:
            skipped.append(SkippedLogin(employee_id=employee.id, employee_code=employee.employee_code,
                                        name=name, reason=reason))
            continue
        to_create.append((employee, username, orphan))

    passwords = [generate_temporary_password() for _ in to_create]
    hashes = _hash_all(passwords)
    created = []
    for (employee, username, orphan), password, hashed in zip(to_create, passwords, hashes):
        user = orphan or AdminUser(username=username, role=ROLE_EMPLOYEE)
        user.employee_id = employee.id
        user.hashed_password = hashed
        user.is_active = True
        user.must_change_password = True
        if orphan is None:
            db.add(user)
        created.append(TemporaryCredential(
            employee_id=employee.id, employee_code=employee.employee_code,
            name=employee_full_name(employee), username=username, temporary_password=password,
        ))
    db.flush()
    for credential in created:
        workflow.reroute_for_manager(db, credential.employee_id)
    db.commit()
    return BulkCreateLoginsResponse(created=created, skipped=skipped)


@router.post("/{employee_id}/reset-password", response_model=TemporaryCredential)
def reset_employee_password(employee_id: int, db: Session = Depends(get_db)):
    employee, user = _get_employee_and_login(db, employee_id)
    password = generate_temporary_password()
    user.hashed_password = hash_password(password)
    user.must_change_password = True
    db.commit()
    return TemporaryCredential(
        employee_id=employee.id, employee_code=employee.employee_code,
        name=employee_full_name(employee), username=user.username, temporary_password=password,
    )


@router.post("/{employee_id}/disable", response_model=EmployeeLoginRow)
def disable_employee_login(
    employee_id: int, db: Session = Depends(get_db), current_user: AdminUser = Depends(require_hr)
):
    employee, user = _get_employee_and_login(db, employee_id)
    if user.id == current_user.id:
        raise HTTPException(status_code=400, detail="You cannot disable your own login.")
    user.is_active = False
    db.commit()
    return _row(employee, user)


@router.post("/{employee_id}/enable", response_model=EmployeeLoginRow)
def enable_employee_login(employee_id: int, db: Session = Depends(get_db)):
    employee, user = _get_employee_and_login(db, employee_id)
    user.is_active = True
    db.flush()
    workflow.reroute_for_manager(db, employee.id)
    db.commit()
    return _row(employee, user)
