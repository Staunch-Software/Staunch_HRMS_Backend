from datetime import date
from typing import Any, Optional

from pydantic import BaseModel


class AdHocItem(BaseModel):
    component_code: str
    amount: float
    remarks: Optional[str] = None


class VariablePayInputCreate(BaseModel):
    employee_id: int
    period_start: date
    period_end: date
    present_days: Optional[float] = None
    lop_days: float = 0
    paid_leave_days: float = 0
    overtime_hours: float = 0
    overtime_amount: Optional[float] = None
    ad_hoc_earnings: list[AdHocItem] = []
    ad_hoc_deductions: list[AdHocItem] = []
    remarks: Optional[str] = None


class VariablePayInputRead(BaseModel):
    id: int
    employee_id: int
    period_start: date
    period_end: date
    source: str
    present_days: Optional[float] = None
    lop_days: float
    paid_leave_days: float
    overtime_hours: float
    overtime_amount: Optional[float] = None
    ad_hoc_earnings: list[Any] = []
    ad_hoc_deductions: list[Any] = []
    remarks: Optional[str] = None
    status: str

    model_config = {"from_attributes": True}
