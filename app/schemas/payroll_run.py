from datetime import date, datetime
from typing import Any, Optional

from pydantic import BaseModel


class PayrollRunCreate(BaseModel):
    pay_schedule_id: int
    period_start: date
    period_end: date
    pay_date: date
    run_type: str = "Regular"  # Regular|OffCycle|FinalSettlement
    created_by: Optional[str] = None


class PayrollRunRead(BaseModel):
    id: int
    pay_schedule_id: int
    period_start: date
    period_end: date
    pay_date: date
    run_type: str
    status: str
    source: str
    total_gross: Optional[float] = None
    total_deductions: Optional[float] = None
    total_net: Optional[float] = None
    employee_count: Optional[int] = None
    calculated_at: Optional[datetime] = None
    approved_at: Optional[datetime] = None
    approved_by: Optional[str] = None
    paid_at: Optional[datetime] = None
    created_by: Optional[str] = None
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class PayrollRunApprove(BaseModel):
    approved_by: Optional[str] = None


class PayrollRunLineItemRead(BaseModel):
    id: int
    payroll_run_id: int
    employee_id: int
    employee_snapshot: dict[str, Any]
    gross_earnings: float
    total_deductions: float
    total_reimbursements: float
    net_pay: float
    pf_wages: Optional[float] = None
    esi_wages: Optional[float] = None
    paid_days: Optional[float] = None
    lop_days: Optional[float] = None
    status: str
    computation_breakdown: list[Any]
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}
