"""Simple CRUD for the statutory config tables — these are admin-configured
settings (versioned by effective_from/to), not business logic. PT data is
seeded with is_verified=False; see db/seed/statutory_seed.py. Also the
per-year employee tax declaration and a TDS projection preview.
"""
import calendar
import re
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
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
    TaxDeclarationForYear,
    TaxDeclarationUpdate,
    TDSConfigCreate,
    TDSConfigRead,
    TDSPreview,
    TDSSlabCreate,
    TDSSlabRead,
)
from app.services.payroll_engine.statutory import tds as tds_calc

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


def _require_employee(db: Session, employee_id: int) -> Employee:
    employee = db.get(Employee, employee_id)
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")
    return employee


def _check_financial_year(financial_year: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}", financial_year) or int(financial_year[5:]) != (int(financial_year[:4]) + 1) % 100:
        raise HTTPException(status_code=400, detail=f"financial_year must look like 2026-27, got '{financial_year}'")


def _optional_float(value) -> float | None:
    return float(value) if value is not None else None


def _declaration_for_year(declaration: EmployeeTaxDeclaration) -> TaxDeclarationForYear:
    return TaxDeclarationForYear(
        id=declaration.id,
        employee_id=declaration.employee_id,
        financial_year=declaration.financial_year,
        regime_choice=declaration.regime_choice if declaration.regime_choice in tds_calc.REGIMES else "New",
        old_regime_deductions=_optional_float(declaration.old_regime_deductions),
        previous_employer_income=_optional_float(declaration.previous_employer_income),
        previous_employer_tds=_optional_float(declaration.previous_employer_tds),
    )


@router.get("/employees/{employee_id}/tax-declaration/{financial_year}", response_model=TaxDeclarationForYear)
def get_employee_tax_declaration(employee_id: int, financial_year: str, db: Session = Depends(get_db)):
    _require_employee(db, employee_id)
    _check_financial_year(financial_year)
    declaration = tds_calc.get_declaration(db, employee_id, financial_year)
    if declaration is None:
        return TaxDeclarationForYear(employee_id=employee_id, financial_year=financial_year)
    return _declaration_for_year(declaration)


@router.put("/employees/{employee_id}/tax-declaration/{financial_year}", response_model=TaxDeclarationForYear)
def put_employee_tax_declaration(
    employee_id: int, financial_year: str, payload: TaxDeclarationUpdate, db: Session = Depends(get_db)
):
    _require_employee(db, employee_id)
    _check_financial_year(financial_year)
    declaration = tds_calc.get_declaration(db, employee_id, financial_year)
    if declaration is None:
        declaration = EmployeeTaxDeclaration(employee_id=employee_id, financial_year=financial_year, declared_investments={})
        db.add(declaration)
    for field, value in payload.model_dump().items():
        setattr(declaration, field, value)
    db.commit()
    db.refresh(declaration)
    return _declaration_for_year(declaration)


@router.get("/employees/{employee_id}/tds-preview", response_model=TDSPreview)
def preview_employee_tds(
    employee_id: int,
    period_end: date | None = None,
    monthly_gross: float | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
):
    """This month's TDS as a payroll run would compute it. Monthly gross is
    `monthly_gross` when given, else the gross of the employee's latest
    calculated line item. period_end defaults to the end of the current month."""
    employee = _require_employee(db, employee_id)
    if period_end is None:
        today = date.today()
        period_end = today.replace(day=calendar.monthrange(today.year, today.month)[1])

    if monthly_gross is not None:
        gross, source = Decimal(str(monthly_gross)), "query"
    else:
        latest = (
            db.query(PayrollRunLineItem)
            .join(PayrollRun, PayrollRunLineItem.payroll_run_id == PayrollRun.id)
            .filter(
                PayrollRunLineItem.employee_id == employee_id,
                PayrollRunLineItem.status == "Included",
                PayrollRun.status.notin_(("Cancelled", "Failed", "Draft", "Processing")),
            )
            .order_by(PayrollRun.period_end.desc(), PayrollRun.id.desc())
            .first()
        )
        gross, source = (Decimal(str(latest.gross_earnings)), "latest_run") if latest else (Decimal("0"), "none")

    result = tds_calc.compute_monthly_tds(db, employee, period_end, gross)
    money = {
        key: float(result[key])
        for key in (
            "current_month_gross", "ytd_gross", "ytd_tds", "previous_employer_income", "previous_employer_tds",
            "annual_projection", "standard_deduction", "old_regime_deductions", "taxable_income", "annual_tax",
            "remaining_tax",
        )
    }
    breakdown = result["tax_breakdown"]
    return TDSPreview(
        employee_id=employee_id,
        period_end=period_end,
        financial_year=result["financial_year"],
        regime=result["regime"],
        has_declaration=result["has_declaration"],
        gross_source=source,
        months_remaining=result["months_remaining"],
        monthly_tds=float(result["amount"]),
        tax_breakdown={k: float(v) for k, v in breakdown.items()} if breakdown else None,
        warning=result["warning"],
        **money,
    )
