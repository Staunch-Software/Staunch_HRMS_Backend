from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.org import Department, Designation, WorkLocation
from app.models.user import AdminUser
from app.schemas.org import (
    DepartmentCreate,
    DepartmentRead,
    DepartmentUpdate,
    DesignationCreate,
    DesignationRead,
    DesignationUpdate,
    WorkLocationCreate,
    WorkLocationRead,
    WorkLocationUpdate,
)

from app.services.leave import workflow
from app.services.reporting import employee_full_name

router = APIRouter(tags=["org"], dependencies=[Depends(get_current_user)])


# --- Departments -----------------------------------------------------------

def _login_employee_ids(db: Session) -> set[int]:
    rows = db.query(AdminUser.employee_id).filter(AdminUser.employee_id.isnot(None), AdminUser.is_active.is_(True))
    return {row[0] for row in rows}


def _department_read(dept: Department, login_ids: set[int]) -> DepartmentRead:
    read = DepartmentRead.model_validate(dept)
    manager = dept.manager
    if manager is not None:
        read.manager_name = employee_full_name(manager) or manager.employee_code
        read.manager_code = manager.employee_code
        read.manager_status = manager.status
        read.manager_has_login = manager.id in login_ids
        if not read.manager_has_login:
            read.warning = f"{read.manager_name} has no login, so approvals will skip them."
    return read


def _saved(db: Session, dept: Department) -> DepartmentRead:
    """Re-routes requests that skipped the manager step now that the
    department may have an eligible manager, then commits."""
    workflow.reroute_pending(db, dept.id)
    db.commit()
    db.refresh(dept)
    return _department_read(dept, _login_employee_ids(db))


def _department_values(db: Session, payload: DepartmentCreate, current: Optional[Department] = None) -> dict:
    values = payload.model_dump()
    if "manager_employee_id" not in payload.model_fields_set:
        values.pop("manager_employee_id")
    manager_id = values.get("manager_employee_id")
    unchanged = current is not None and manager_id == current.manager_employee_id
    if manager_id is not None and not unchanged:
        manager = db.get(Employee, manager_id)
        if manager is None or (manager.status or "").strip().lower() != "active":
            raise HTTPException(status_code=400, detail="The department manager must be an active employee.")
    return values


@router.get("/departments", response_model=list[DepartmentRead])
def list_departments(db: Session = Depends(get_db)):
    login_ids = _login_employee_ids(db)
    return [_department_read(d, login_ids) for d in db.query(Department).order_by(Department.name).all()]


@router.post("/departments", response_model=DepartmentRead, status_code=status.HTTP_201_CREATED)
def create_department(payload: DepartmentCreate, db: Session = Depends(get_db)):
    if db.query(Department).filter(Department.code == payload.code).first():
        raise HTTPException(status_code=400, detail=f"Department code '{payload.code}' already exists.")
    dept = Department(**_department_values(db, payload))
    db.add(dept)
    db.flush()
    return _saved(db, dept)


@router.put("/departments/{department_id}", response_model=DepartmentRead)
def update_department(department_id: int, payload: DepartmentUpdate, db: Session = Depends(get_db)):
    dept = db.get(Department, department_id)
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    if db.query(Department).filter(Department.code == payload.code, Department.id != department_id).first():
        raise HTTPException(status_code=400, detail=f"Department code '{payload.code}' already exists.")
    for field, value in _department_values(db, payload, dept).items():
        setattr(dept, field, value)
    db.flush()
    return _saved(db, dept)


# --- Designations ------------------------------------------------------------

@router.get("/designations", response_model=list[DesignationRead])
def list_designations(db: Session = Depends(get_db)):
    return db.query(Designation).order_by(Designation.name).all()


@router.post("/designations", response_model=DesignationRead, status_code=status.HTTP_201_CREATED)
def create_designation(payload: DesignationCreate, db: Session = Depends(get_db)):
    designation = Designation(**payload.model_dump())
    db.add(designation)
    db.commit()
    db.refresh(designation)
    return designation


@router.put("/designations/{designation_id}", response_model=DesignationRead)
def update_designation(designation_id: int, payload: DesignationUpdate, db: Session = Depends(get_db)):
    designation = db.get(Designation, designation_id)
    if not designation:
        raise HTTPException(status_code=404, detail="Designation not found")
    for field, value in payload.model_dump().items():
        setattr(designation, field, value)
    db.commit()
    db.refresh(designation)
    return designation


# --- Work Locations ----------------------------------------------------------

@router.get("/work-locations", response_model=list[WorkLocationRead])
def list_work_locations(db: Session = Depends(get_db)):
    return db.query(WorkLocation).order_by(WorkLocation.name).all()


@router.post("/work-locations", response_model=WorkLocationRead, status_code=status.HTTP_201_CREATED)
def create_work_location(payload: WorkLocationCreate, db: Session = Depends(get_db)):
    location = WorkLocation(**payload.model_dump())
    db.add(location)
    db.commit()
    db.refresh(location)
    return location


@router.put("/work-locations/{location_id}", response_model=WorkLocationRead)
def update_work_location(location_id: int, payload: WorkLocationUpdate, db: Session = Depends(get_db)):
    location = db.get(WorkLocation, location_id)
    if not location:
        raise HTTPException(status_code=404, detail="Work location not found")
    for field, value in payload.model_dump().items():
        setattr(location, field, value)
    db.commit()
    db.refresh(location)
    return location
