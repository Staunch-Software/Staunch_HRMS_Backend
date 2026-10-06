"""Simple CRUD for the statutory config tables — these are admin-configured
settings (versioned by effective_from/to), not business logic. PT and TDS
data is seeded with is_verified=False; see db/seed/statutory_seed.py.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.statutory import ESIConfig, EmployeeTaxDeclaration, PFConfig, PTSlab, PTState, TDSConfig, TDSSlab
from app.schemas.statutory import (
    ESIConfigCreate,
    ESIConfigRead,
    EmployeeTaxDeclarationCreate,
    EmployeeTaxDeclarationRead,
    PFConfigCreate,
    PFConfigRead,
    PTSlabCreate,
    PTSlabRead,
    PTStateCreate,
    PTStateRead,
    TDSConfigCreate,
    TDSConfigRead,
    TDSSlabCreate,
    TDSSlabRead,
)

router = APIRouter(tags=["statutory"], dependencies=[Depends(get_current_user)])


# --- PF config ---------------------------------------------------------------

@router.get("/statutory/pf-config", response_model=list[PFConfigRead])
def list_pf_configs(db: Session = Depends(get_db)):
    return db.query(PFConfig).order_by(PFConfig.effective_from.desc()).all()


@router.post("/statutory/pf-config", response_model=PFConfigRead, status_code=status.HTTP_201_CREATED)
def create_pf_config(payload: PFConfigCreate, db: Session = Depends(get_db)):
    config = PFConfig(**payload.model_dump())
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


@router.put("/statutory/pf-config/{config_id}", response_model=PFConfigRead)
def update_pf_config(config_id: int, payload: PFConfigCreate, db: Session = Depends(get_db)):
    config = db.get(PFConfig, config_id)
    if not config:
        raise HTTPException(status_code=404, detail="PF config not found")
    for field, value in payload.model_dump().items():
        setattr(config, field, value)
    db.commit()
    db.refresh(config)
    return config


# --- ESI config ---------------------------------------------------------------

@router.get("/statutory/esi-config", response_model=list[ESIConfigRead])
def list_esi_configs(db: Session = Depends(get_db)):
    return db.query(ESIConfig).order_by(ESIConfig.effective_from.desc()).all()


@router.post("/statutory/esi-config", response_model=ESIConfigRead, status_code=status.HTTP_201_CREATED)
def create_esi_config(payload: ESIConfigCreate, db: Session = Depends(get_db)):
    config = ESIConfig(**payload.model_dump())
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


@router.put("/statutory/esi-config/{config_id}", response_model=ESIConfigRead)
def update_esi_config(config_id: int, payload: ESIConfigCreate, db: Session = Depends(get_db)):
    config = db.get(ESIConfig, config_id)
    if not config:
        raise HTTPException(status_code=404, detail="ESI config not found")
    for field, value in payload.model_dump().items():
        setattr(config, field, value)
    db.commit()
    db.refresh(config)
    return config


# --- PT states / slabs --------------------------------------------------------

@router.get("/statutory/pt-states", response_model=list[PTStateRead])
def list_pt_states(db: Session = Depends(get_db)):
    return db.query(PTState).order_by(PTState.name).all()


@router.post("/statutory/pt-states", response_model=PTStateRead, status_code=status.HTTP_201_CREATED)
def create_pt_state(payload: PTStateCreate, db: Session = Depends(get_db)):
    if db.query(PTState).filter(PTState.code == payload.code).first():
        raise HTTPException(status_code=400, detail=f"PT state code '{payload.code}' already exists.")
    state = PTState(**payload.model_dump())
    db.add(state)
    db.commit()
    db.refresh(state)
    return state


@router.get("/statutory/pt-slabs", response_model=list[PTSlabRead])
def list_pt_slabs(state_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(PTSlab)
    if state_id is not None:
        query = query.filter(PTSlab.state_id == state_id)
    return query.order_by(PTSlab.state_id, PTSlab.income_min).all()


@router.post("/statutory/pt-slabs", response_model=PTSlabRead, status_code=status.HTTP_201_CREATED)
def create_pt_slab(payload: PTSlabCreate, db: Session = Depends(get_db)):
    if not db.get(PTState, payload.state_id):
        raise HTTPException(status_code=404, detail="PT state not found")
    slab = PTSlab(**payload.model_dump())
    db.add(slab)
    db.commit()
    db.refresh(slab)
    return slab


@router.put("/statutory/pt-slabs/{slab_id}", response_model=PTSlabRead)
def update_pt_slab(slab_id: int, payload: PTSlabCreate, db: Session = Depends(get_db)):
    slab = db.get(PTSlab, slab_id)
    if not slab:
        raise HTTPException(status_code=404, detail="PT slab not found")
    for field, value in payload.model_dump().items():
        setattr(slab, field, value)
    db.commit()
    db.refresh(slab)
    return slab


# --- TDS slabs / config --------------------------------------------------------

@router.get("/statutory/tds-slabs", response_model=list[TDSSlabRead])
def list_tds_slabs(financial_year: str | None = None, db: Session = Depends(get_db)):
    query = db.query(TDSSlab)
    if financial_year:
        query = query.filter(TDSSlab.financial_year == financial_year)
    return query.order_by(TDSSlab.regime, TDSSlab.income_from).all()


@router.post("/statutory/tds-slabs", response_model=TDSSlabRead, status_code=status.HTTP_201_CREATED)
def create_tds_slab(payload: TDSSlabCreate, db: Session = Depends(get_db)):
    slab = TDSSlab(**payload.model_dump())
    db.add(slab)
    db.commit()
    db.refresh(slab)
    return slab


@router.get("/statutory/tds-config", response_model=list[TDSConfigRead])
def list_tds_configs(db: Session = Depends(get_db)):
    return db.query(TDSConfig).order_by(TDSConfig.financial_year.desc()).all()


@router.post("/statutory/tds-config", response_model=TDSConfigRead, status_code=status.HTTP_201_CREATED)
def create_tds_config(payload: TDSConfigCreate, db: Session = Depends(get_db)):
    config = TDSConfig(**payload.model_dump())
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


# --- Employee tax declarations -------------------------------------------------

@router.get("/employees/{employee_id}/tax-declaration", response_model=list[EmployeeTaxDeclarationRead])
def list_employee_tax_declarations(employee_id: int, db: Session = Depends(get_db)):
    if not db.get(Employee, employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    return (
        db.query(EmployeeTaxDeclaration)
        .filter(EmployeeTaxDeclaration.employee_id == employee_id)
        .order_by(EmployeeTaxDeclaration.financial_year.desc())
        .all()
    )


@router.post(
    "/employees/{employee_id}/tax-declaration",
    response_model=EmployeeTaxDeclarationRead,
    status_code=status.HTTP_201_CREATED,
)
def upsert_employee_tax_declaration(
    employee_id: int, payload: EmployeeTaxDeclarationCreate, db: Session = Depends(get_db)
):
    if not db.get(Employee, employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")
    existing = (
        db.query(EmployeeTaxDeclaration)
        .filter(
            EmployeeTaxDeclaration.employee_id == employee_id,
            EmployeeTaxDeclaration.financial_year == payload.financial_year,
        )
        .first()
    )
    if existing:
        for field, value in payload.model_dump().items():
            setattr(existing, field, value)
        db.commit()
        db.refresh(existing)
        return existing

    declaration = EmployeeTaxDeclaration(employee_id=employee_id, **payload.model_dump())
    db.add(declaration)
    db.commit()
    db.refresh(declaration)
    return declaration
