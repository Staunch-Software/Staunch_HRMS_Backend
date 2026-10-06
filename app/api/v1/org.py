from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.org import Department, Designation, WorkLocation
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

router = APIRouter(tags=["org"], dependencies=[Depends(get_current_user)])


# --- Departments -----------------------------------------------------------

@router.get("/departments", response_model=list[DepartmentRead])
def list_departments(db: Session = Depends(get_db)):
    return db.query(Department).order_by(Department.name).all()


@router.post("/departments", response_model=DepartmentRead, status_code=status.HTTP_201_CREATED)
def create_department(payload: DepartmentCreate, db: Session = Depends(get_db)):
    if db.query(Department).filter(Department.code == payload.code).first():
        raise HTTPException(status_code=400, detail=f"Department code '{payload.code}' already exists.")
    dept = Department(**payload.model_dump())
    db.add(dept)
    db.commit()
    db.refresh(dept)
    return dept


@router.put("/departments/{department_id}", response_model=DepartmentRead)
def update_department(department_id: int, payload: DepartmentUpdate, db: Session = Depends(get_db)):
    dept = db.get(Department, department_id)
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    if db.query(Department).filter(Department.code == payload.code, Department.id != department_id).first():
        raise HTTPException(status_code=400, detail=f"Department code '{payload.code}' already exists.")
    for field, value in payload.model_dump().items():
        setattr(dept, field, value)
    db.commit()
    db.refresh(dept)
    return dept


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
