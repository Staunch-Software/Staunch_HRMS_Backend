from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel


class PayGroupRead(BaseModel):
    id: int
    code: str
    name: str
    calc_model: str
    # FORMULA groups: the legacy params they were converted from, overlaid with
    # the definition's settings and constants (lower-case keys), read-only.
    params: dict[str, Any]
    description: Optional[str] = None
    is_active: bool
    # Legacy strategies only: {key: {"type", "description", "choices"?}}; {} for FORMULA.
    param_spec: dict[str, Any] = {}
    member_count: int = 0
    employees_count: int = 0
    version: int = 0
    lines_count: int = 0
    standard_days: Optional[float] = None
    attendance_source: Optional[str] = None
    tds_enabled: Optional[bool] = None
    net_rounding: Optional[str] = None
    # Detail only (GET /pay-groups/{id} and write responses).
    definition: Optional[dict[str, Any]] = None
    # Pay-profile fields the formulas read, e.g. ["daily_wage", "skill_level"].
    profile_fields: list[str] = []
    # Skill levels the group's skill lookups know (row keys).
    skill_levels: list[str] = []
    employee_fields: list[dict[str, Any]] = []
    created_at: datetime
    updated_at: datetime


class PayGroupCreate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    # Copy the definition of this pay group (id); else `definition`; else a starter definition.
    template: Optional[int] = None
    definition: Optional[dict[str, Any]] = None
    note: Optional[str] = None
    is_active: bool = True


class PayGroupDuplicate(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    note: Optional[str] = None


class PayGroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    # FORMULA groups: a new definition is validated and saved as the next version.
    definition: Optional[dict[str, Any]] = None
    note: Optional[str] = None
    # Optional optimistic lock: 409 when the group's version moved on.
    expected_version: Optional[int] = None
    # Legacy (non-FORMULA) groups only.
    params: Optional[dict[str, Any]] = None
    is_active: Optional[bool] = None


class PayGroupStatusUpdate(BaseModel):
    is_active: bool
    # Deactivate even though employees still have pay profiles in this group.
    force: bool = False


class DefinitionValidateRequest(BaseModel):
    definition: dict[str, Any]


class PayGroupTestRequest(BaseModel):
    # Unsaved definition to test; omitted = the group's saved definition.
    definition: Optional[dict[str, Any]] = None
    employee_id: Optional[int] = None
    # Over the employee's pay profile (or from scratch): monthly_salary, daily_wage,
    # skill_level, pf_applicable, esi_applicable, pf_cap, fixed_monthly_amount, extra.
    profile: Optional[dict[str, Any]] = None
    # {"DAYS_PRESENT": 24, "LOP_DAYS": 2, "OT_HOURS": 4, "OT_DAYS": 0, "PAID_LEAVE_DAYS": 0,
    #  "<input code>": amount, "HAS_INPUT": 1}; omitted = the employee's monthly input for `month`.
    inputs: Optional[dict[str, Any]] = None
    month: Optional[str] = None  # YYYY-MM, default this month
    include_tds: bool = True


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
    # Employee custom fields of the group (definition.employee_fields), by key.
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
