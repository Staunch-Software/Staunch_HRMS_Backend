from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel


class PayGroupRead(BaseModel):
    id: int
    code: str
    name: str
    calc_model: str
    params: dict[str, Any]
    description: Optional[str] = None
    is_active: bool
    # {key: {"type": number|skill_table|bool|choice, "description": str, "choices"?: [...]}}
    param_spec: dict[str, Any] = {}
    member_count: int = 0
    created_at: datetime
    updated_at: datetime


class PayGroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    params: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None


class EmployeePayProfileUpsert(BaseModel):
    pay_group_id: int
    effective_from: date
    monthly_salary: Optional[float] = None
    daily_wage: Optional[float] = None
    skill_level: Optional[str] = None
    pf_applicable: Optional[bool] = None
    esi_applicable: Optional[bool] = None
    pf_cap: Optional[float] = None
    fixed_monthly_amount: Optional[float] = None
    extra: dict[str, Any] = {}


class EmployeePayProfileRead(BaseModel):
    id: int
    employee_id: int
    pay_group_id: int
    pay_group_code: Optional[str] = None
    pay_group_name: Optional[str] = None
    calc_model: Optional[str] = None
    monthly_salary: Optional[float] = None
    daily_wage: Optional[float] = None
    skill_level: Optional[str] = None
    pf_applicable: Optional[bool] = None
    esi_applicable: Optional[bool] = None
    pf_cap: Optional[float] = None
    fixed_monthly_amount: Optional[float] = None
    extra: dict[str, Any] = {}
    effective_from: date
    effective_to: Optional[date] = None
    created_at: datetime
    updated_at: datetime
