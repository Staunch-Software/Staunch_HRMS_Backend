from datetime import date
from typing import Optional

from pydantic import BaseModel


class SalaryStructureAssignmentCreate(BaseModel):
    salary_component_id: int
    value_override: Optional[float] = None
    effective_from: date


class SalaryStructureAssignmentRead(BaseModel):
    id: int
    employee_id: int
    salary_component_id: int
    value_override: Optional[float] = None
    effective_from: date
    effective_to: Optional[date] = None

    model_config = {"from_attributes": True}


class SalaryStructureAssignmentResolved(SalaryStructureAssignmentRead):
    """A resolved row enriched with the component's code/name for display,
    so the frontend doesn't need a second round-trip to salary-components."""

    component_code: str
    component_name: str
    component_type: str
