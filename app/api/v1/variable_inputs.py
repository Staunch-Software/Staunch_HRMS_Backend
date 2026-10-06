from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.models.variable_pay import VariablePayInput
from app.schemas.variable_pay import VariablePayInputCreate, VariablePayInputRead

router = APIRouter(tags=["variable-inputs"], dependencies=[Depends(get_current_user)])


@router.get("/variable-inputs", response_model=list[VariablePayInputRead])
def list_variable_inputs(
    period_start: date | None = None, period_end: date | None = None, db: Session = Depends(get_db)
):
    query = db.query(VariablePayInput)
    if period_start is not None:
        query = query.filter(VariablePayInput.period_start == period_start)
    if period_end is not None:
        query = query.filter(VariablePayInput.period_end == period_end)
    return query.order_by(VariablePayInput.employee_id).all()


@router.post("/variable-inputs", response_model=VariablePayInputRead, status_code=status.HTTP_201_CREATED)
def upsert_variable_input(payload: VariablePayInputCreate, db: Session = Depends(get_db)):
    """Upserts one row per (employee, period_start, period_end). Rejects with
    409 if the existing row for that period is already Locked (a run has
    consumed it) — locked rows must stay reproducible for past runs."""
    if not db.get(Employee, payload.employee_id):
        raise HTTPException(status_code=404, detail="Employee not found")

    existing = (
        db.query(VariablePayInput)
        .filter(
            VariablePayInput.employee_id == payload.employee_id,
            VariablePayInput.period_start == payload.period_start,
            VariablePayInput.period_end == payload.period_end,
        )
        .first()
    )

    data = payload.model_dump()
    data["ad_hoc_earnings"] = [item for item in data.get("ad_hoc_earnings", [])]
    data["ad_hoc_deductions"] = [item for item in data.get("ad_hoc_deductions", [])]

    if existing:
        if existing.status == "Locked":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "This variable input is Locked — a payroll run has already consumed it "
                    "for this period. It cannot be edited or replaced."
                ),
            )
        for field, value in data.items():
            if field == "employee_id":
                continue
            setattr(existing, field, value)
        db.commit()
        db.refresh(existing)
        return existing

    row = VariablePayInput(source="Manual", status="Draft", **data)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row
