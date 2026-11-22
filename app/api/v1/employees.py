from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.crypto import is_masked
from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee, EmployeeBankAccount
from app.models.salary_component import SalaryComponent
from app.models.salary_structure import EmployeeSalaryStructureAssignment
from app.schemas.employee import (
    EmployeeBankAccountRead,
    EmployeeBankAccountUpdate,
    EmployeeCreate,
    EmployeeRead,
    EmployeeSensitiveRead,
    EmployeeStatusUpdate,
    EmployeeUpdate,
)
from app.schemas.reporting import EmployeeBrief
from app.services.reporting import reporting_manager_error

router = APIRouter(tags=["employees"], dependencies=[Depends(get_current_user)])

# Returned masked by the API; a null or masked value on write keeps what is stored.
SENSITIVE_EMPLOYEE_FIELDS = ("aadhaar_number", "pan_number")


def _keeps_stored(value) -> bool:
    return value is None or is_masked(value)


def _auto_create_basic_assignment(db: Session, employee: Employee) -> None:
    """On employee creation, auto-create a BASIC salary structure assignment
    from employee.basic_salary if provided and a BASIC component exists —
    closes the gap where employees weren't linked to real salary components."""
    if employee.basic_salary is None:
        return
    basic_component = db.query(SalaryComponent).filter(SalaryComponent.code == "BASIC").first()
    if not basic_component:
        return
    effective_from = employee.date_of_joining or date.today()
    db.add(
        EmployeeSalaryStructureAssignment(
            employee_id=employee.id,
            salary_component_id=basic_component.id,
            value_override=employee.basic_salary,
            effective_from=effective_from,
            effective_to=None,
        )
    )
    db.commit()


@router.get("/employees", response_model=list[EmployeeRead])
def list_employees(db: Session = Depends(get_db)):
    return db.query(Employee).order_by(Employee.id).all()


@router.post("/employees", response_model=EmployeeRead, status_code=status.HTTP_201_CREATED)
def create_employee(payload: EmployeeCreate, db: Session = Depends(get_db)):
    if db.query(Employee).filter(Employee.employee_code == payload.employee_code).first():
        raise HTTPException(
            status_code=400, detail=f"Employee ID '{payload.employee_code}' already exists."
        )
    manager_error = reporting_manager_error(db, None, payload.reporting_manager_id)
    if manager_error:
        raise HTTPException(status_code=400, detail=manager_error)
    data = payload.model_dump()
    for field in SENSITIVE_EMPLOYEE_FIELDS:
        if _keeps_stored(data.get(field)):
            data[field] = None
    employee = Employee(**data)
    db.add(employee)
    db.commit()
    db.refresh(employee)
    _auto_create_basic_assignment(db, employee)
    return employee


@router.get("/employees/{employee_id}", response_model=EmployeeRead)
def get_employee(employee_id: int, db: Session = Depends(get_db)):
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


@router.get("/employees/{employee_id}/direct-reports", response_model=list[EmployeeBrief])
def list_direct_reports(employee_id: int, db: Session = Depends(get_db)):
    if not db.get(Employee, employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    reports = (
        db.query(Employee)
        .filter(Employee.reporting_manager_id == employee_id)
        .order_by(Employee.employee_code)
        .all()
    )
    return [EmployeeBrief.from_employee(e) for e in reports]


@router.put("/employees/{employee_id}", response_model=EmployeeRead)
def update_employee(employee_id: int, payload: EmployeeUpdate, db: Session = Depends(get_db)):
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    duplicate = (
        db.query(Employee)
        .filter(Employee.employee_code == payload.employee_code, Employee.id != employee_id)
        .first()
    )
    if duplicate:
        raise HTTPException(
            status_code=400, detail=f"Employee ID '{payload.employee_code}' already exists."
        )
    if "reporting_manager_id" in payload.model_fields_set:
        manager_error = reporting_manager_error(db, employee_id, payload.reporting_manager_id)
        if manager_error:
            raise HTTPException(status_code=400, detail=manager_error)
    # exclude_unset: a client that doesn't know about a field (e.g. an older
    # form without the master-data fields) must not wipe it.
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field in SENSITIVE_EMPLOYEE_FIELDS and _keeps_stored(value):
            continue
        setattr(employee, field, value)
    db.commit()
    db.refresh(employee)
    return employee


@router.patch("/employees/{employee_id}/status", response_model=EmployeeRead)
def update_employee_status(employee_id: int, payload: EmployeeStatusUpdate, db: Session = Depends(get_db)):
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    employee.status = payload.status
    db.commit()
    db.refresh(employee)
    return employee


@router.delete("/employees/{employee_id}")
def delete_employee(employee_id: int, db: Session = Depends(get_db)):
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    db.delete(employee)
    db.commit()
    return {"success": True}


# --- Bank account ------------------------------------------------------------

def _get_primary_bank_account(db: Session, employee_id: int) -> EmployeeBankAccount | None:
    return (
        db.query(EmployeeBankAccount)
        .filter(EmployeeBankAccount.employee_id == employee_id, EmployeeBankAccount.is_active.is_(True))
        .order_by(EmployeeBankAccount.id.desc())
        .first()
    )


@router.get("/employees/{employee_id}/bank-account", response_model=EmployeeBankAccountRead)
def get_bank_account(employee_id: int, db: Session = Depends(get_db)):
    if not db.get(Employee, employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    account = _get_primary_bank_account(db, employee_id)
    if not account:
        raise HTTPException(status_code=404, detail="Bank account not found for this employee")
    return account


@router.put("/employees/{employee_id}/bank-account", response_model=EmployeeBankAccountRead)
def upsert_bank_account(
    employee_id: int,
    payload: EmployeeBankAccountUpdate,
    db: Session = Depends(get_db),
):
    if not db.get(Employee, employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    account = _get_primary_bank_account(db, employee_id)
    data = payload.model_dump()
    if _keeps_stored(data["account_number"]):
        if not account:
            raise HTTPException(status_code=422, detail="Account Number is required.")
        data.pop("account_number")
    if account:
        for field, value in data.items():
            setattr(account, field, value)
    else:
        account = EmployeeBankAccount(employee_id=employee_id, **data)
        db.add(account)
    db.commit()
    db.refresh(account)
    return account


@router.get("/employees/{employee_id}/sensitive", response_model=EmployeeSensitiveRead)
def get_employee_sensitive(employee_id: int, db: Session = Depends(get_db)):
    """Unmasked Aadhaar, PAN and primary account number, for the HR edit form's reveal."""
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    account = _get_primary_bank_account(db, employee_id)
    return EmployeeSensitiveRead(
        employee_id=employee.id,
        aadhaar_number=employee.aadhaar_number,
        pan_number=employee.pan_number,
        account_number=account.account_number if account else None,
    )
