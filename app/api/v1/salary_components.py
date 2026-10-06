import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.salary_component import SalaryComponent
from app.schemas.salary_component import SalaryComponentCreate, SalaryComponentRead, SalaryComponentUpdate
from app.services.formula.dependency_graph import build_dependency_graph, topological_sort
from app.services.formula.evaluator import FormulaEvaluationError, evaluate_formula

router = APIRouter(tags=["salary-components"], dependencies=[Depends(get_current_user)])


class FormulaValidationRequest(BaseModel):
    value: str


class FormulaValidationResponse(BaseModel):
    valid: bool
    error: str | None = None


@router.get("/salary-components", response_model=list[SalaryComponentRead])
def list_salary_components(db: Session = Depends(get_db)):
    return db.query(SalaryComponent).order_by(SalaryComponent.payslip_order, SalaryComponent.id).all()


@router.post("/salary-components", response_model=SalaryComponentRead, status_code=status.HTTP_201_CREATED)
def create_salary_component(payload: SalaryComponentCreate, db: Session = Depends(get_db)):
    if db.query(SalaryComponent).filter(SalaryComponent.code == payload.code).first():
        raise HTTPException(status_code=400, detail=f"Component code '{payload.code}' already exists.")
    component = SalaryComponent(**payload.model_dump())
    db.add(component)
    db.commit()
    db.refresh(component)
    return component


@router.put("/salary-components/{component_id}", response_model=SalaryComponentRead)
def update_salary_component(component_id: int, payload: SalaryComponentUpdate, db: Session = Depends(get_db)):
    component = db.get(SalaryComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Salary component not found")
    duplicate = (
        db.query(SalaryComponent)
        .filter(SalaryComponent.code == payload.code, SalaryComponent.id != component_id)
        .first()
    )
    if duplicate:
        raise HTTPException(status_code=400, detail=f"Component code '{payload.code}' already exists.")
    for field, value in payload.model_dump().items():
        setattr(component, field, value)
    db.commit()
    db.refresh(component)
    return component


@router.post("/salary-components/validate-formula", response_model=FormulaValidationResponse)
def validate_formula(payload: FormulaValidationRequest, db: Session = Depends(get_db)):
    """Dry-run validates a formula string against the current active
    component codes: parses its dependencies (rejecting a circular
    reference) and evaluates it against a dummy namespace of zeros, so
    SalaryComponentForm.js can validate a formula before saving."""
    known_codes = {c.code for c, in db.query(SalaryComponent.code).all()} | {
        "PF_WAGES",
        "ESI_WAGES",
        "GROSS_EARNINGS",
    }
    referenced = {
        code
        for code in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", payload.value)
        if code in known_codes
    }
    dummy_component = [{"code": "_CANDIDATE", "calculation_method": "Percentage/Formula", "value": payload.value}]
    dummy_component += [{"code": code, "calculation_method": "Fixed Amount", "value": "0"} for code in referenced]

    try:
        graph = build_dependency_graph(dummy_component)
        topological_sort(graph)
    except ValueError as exc:
        return FormulaValidationResponse(valid=False, error=str(exc))

    dummy_namespace = {code: 0.0 for code in referenced}
    try:
        evaluate_formula(payload.value, dummy_namespace)
    except FormulaEvaluationError as exc:
        return FormulaValidationResponse(valid=False, error=str(exc))

    return FormulaValidationResponse(valid=True, error=None)


@router.delete("/salary-components/{component_id}")
def delete_salary_component(component_id: int, db: Session = Depends(get_db)):
    component = db.get(SalaryComponent, component_id)
    if not component:
        raise HTTPException(status_code=404, detail="Salary component not found for deletion")
    if component.code == "BASIC":
        raise HTTPException(status_code=400, detail="Cannot delete the core 'BASIC' component.")
    db.delete(component)
    db.commit()
    return {"success": True}
