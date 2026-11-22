"""Shared lookups for VariablePayInput writers (payroll Excel import,
attendance uploads). A Locked row has been consumed by a payroll run and
must never be replaced."""
from datetime import date
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.variable_pay import VariablePayInput


def find_input(db: Session, employee_id: int, period_start: date, period_end: date) -> Optional[VariablePayInput]:
    return (
        db.query(VariablePayInput)
        .filter(VariablePayInput.employee_id == employee_id, VariablePayInput.period_start == period_start,
                VariablePayInput.period_end == period_end)
        .first()
    )


def locked_employee_ids(db: Session, employee_ids: Iterable[int], period_start: date, period_end: date) -> set[int]:
    ids = list(set(employee_ids))
    if not ids:
        return set()
    rows = db.query(VariablePayInput.employee_id).filter(
        VariablePayInput.employee_id.in_(ids),
        VariablePayInput.period_start == period_start,
        VariablePayInput.period_end == period_end,
        VariablePayInput.status == "Locked",
    )
    return {employee_id for (employee_id,) in rows}
