import datetime as dt
from datetime import date, datetime, time
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_serializer, field_validator, model_validator

from app.models.leave import CREDIT_MODES, CREDIT_MONTHLY_TIERED, HALF_FULL


def _json_number(value: Optional[Decimal]):
    if value is None:
        return None
    return int(value) if value == value.to_integral_value() else float(value)


class ServiceTier(BaseModel):
    min_service_months: int = Field(ge=0, le=600)
    days_per_month: Decimal = Field(ge=0, le=31)
    carry_forward: bool = False
    carry_forward_limit: Optional[Decimal] = Field(default=None, ge=0, le=366)

    @field_serializer("days_per_month", "carry_forward_limit")
    def _number(self, value: Optional[Decimal]):
        return _json_number(value)


class LeaveTypeBase(BaseModel):
    code: str = Field(min_length=1, max_length=10)
    name: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=500)
    annual_entitlement: Decimal = Field(default=Decimal("0"), ge=0, le=366)
    credit_mode: str = "YEARLY_UPFRONT"
    pro_rata_for_joiners: bool = True
    carry_forward: bool = False
    carry_forward_limit: Optional[Decimal] = Field(default=None, ge=0, le=366)
    is_paid: bool = True
    allow_half_day: bool = True
    sandwich_rule: bool = True
    allowed_during_notice: bool = False
    allow_negative_balance: bool = False
    service_tiers: Optional[list[ServiceTier]] = None
    joining_cutoff_day: int = Field(default=15, ge=1, le=31)
    is_active: bool = True
    sort_order: int = 0

    @model_validator(mode="after")
    def _tiers(self):
        if self.credit_mode != CREDIT_MONTHLY_TIERED:
            return self
        if not self.service_tiers:
            raise ValueError("Monthly by length of service needs at least one service tier")
        mins = [t.min_service_months for t in self.service_tiers]
        if len(set(mins)) != len(mins):
            raise ValueError("Each service tier needs a different 'from months of service'")
        self.service_tiers = sorted(self.service_tiers, key=lambda t: t.min_service_months)
        for tier in self.service_tiers:
            if not tier.carry_forward:
                tier.carry_forward_limit = None
        return self

    @field_validator("code")
    @classmethod
    def _code(cls, value: str) -> str:
        value = value.strip().upper()
        if not value.replace("_", "").isalnum():
            raise ValueError("Code may contain only letters, digits and underscores")
        return value

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value

    @field_validator("credit_mode")
    @classmethod
    def _credit_mode(cls, value: str) -> str:
        value = value.upper()
        if value not in CREDIT_MODES:
            raise ValueError(f"credit_mode must be one of {', '.join(CREDIT_MODES)}")
        return value


class LeaveTypeCreate(LeaveTypeBase):
    pass


class LeaveTypeUpdate(LeaveTypeBase):
    pass


class LeaveTypeRead(LeaveTypeBase):
    id: int

    model_config = {"from_attributes": True}


class ActionComment(BaseModel):
    comment: Optional[str] = Field(default=None, max_length=1000)


class LeaveApplyRequest(BaseModel):
    leave_type_id: int
    from_date: date
    to_date: date
    from_half: str = HALF_FULL
    to_half: str = HALF_FULL
    reason: Optional[str] = Field(default=None, max_length=1000)


class LeaveRecordRequest(LeaveApplyRequest):
    employee_id: int
    comment: Optional[str] = Field(default=None, max_length=1000)


class PermissionApplyRequest(BaseModel):
    date: dt.date
    from_time: time
    to_time: time
    reason: Optional[str] = Field(default=None, max_length=1000)


class PermissionRecordRequest(PermissionApplyRequest):
    employee_id: int
    comment: Optional[str] = Field(default=None, max_length=1000)
    override_limits: bool = False


class RequestBase(BaseModel):
    id: int
    employee_id: int
    employee_code: Optional[str] = None
    employee_name: Optional[str] = None
    status: str
    step: str
    reason: Optional[str] = None
    recorded_by_hr: bool = False
    applied_by: Optional[str] = None
    manager_id: Optional[int] = None
    manager_name: Optional[str] = None
    manager_action_by: Optional[str] = None
    manager_action_at: Optional[datetime] = None
    manager_comment: Optional[str] = None
    hr_action_by: Optional[str] = None
    hr_action_at: Optional[datetime] = None
    hr_comment: Optional[str] = None
    cancelled_by: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    cancel_comment: Optional[str] = None
    created_at: Optional[datetime] = None
    warnings: list[str] = []


class LeaveApplicationRead(RequestBase):
    leave_type_id: int
    leave_type_code: Optional[str] = None
    leave_type_name: Optional[str] = None
    from_date: date
    to_date: date
    from_half: str
    to_half: str
    days: Decimal


class PermissionRequestRead(RequestBase):
    date: dt.date
    from_time: time
    to_time: time
    minutes: int
    override_limits: bool = False


class LeaveDayRead(BaseModel):
    date: dt.date
    kind: str
    holiday_name: Optional[str] = None
    counted: Decimal
    half: str
    sandwiched: bool = False


class LeaveDaysPreview(BaseModel):
    days: Decimal
    breakdown: list[LeaveDayRead]
    leave_year: int
    balance: Optional[Decimal] = None
    available: Optional[Decimal] = None
    tracks_balance: bool
    errors: list[str] = []


class BalanceRow(BaseModel):
    leave_type_id: int
    leave_type_code: str
    leave_type_name: str
    tracks_balance: bool
    carried_forward: Decimal = Decimal("0")
    credited: Decimal = Decimal("0")
    adjusted: Decimal = Decimal("0")
    used: Decimal = Decimal("0")
    late_deducted: Decimal = Decimal("0")
    lapsed: Decimal = Decimal("0")
    balance: Decimal = Decimal("0")
    pending: Decimal = Decimal("0")
    available: Decimal = Decimal("0")
    # Monthly-by-service types: the employee's tier this month.
    tier_label: Optional[str] = None
    # The selected month ("YYYY-MM"): its own credit, leave dated in it,
    # pending leave starting in it, and what is usable for leave in it.
    month: Optional[str] = None
    month_credited: Decimal = Decimal("0")
    month_used: Decimal = Decimal("0")
    month_pending: Decimal = Decimal("0")
    month_available: Decimal = Decimal("0")


class EmployeeBalances(BaseModel):
    employee_id: int
    employee_code: str
    employee_name: Optional[str] = None
    year: int
    month: Optional[str] = None
    balances: list[BalanceRow]


class BalanceAdjustRequest(BaseModel):
    employee_id: int
    leave_type_id: int
    year: Optional[int] = None
    days: Decimal = Field(ge=-366, le=366)
    note: str = Field(min_length=1, max_length=500)
    # CARRY_FORWARD: the opening carry forward entered by HR.
    entry_type: Literal["ADJUSTMENT", "CARRY_FORWARD"] = "ADJUSTMENT"

    @field_validator("days")
    @classmethod
    def _non_zero(cls, value: Decimal) -> Decimal:
        if value == 0:
            raise ValueError("Adjustment days cannot be zero")
        return value


class CreditRunRequest(BaseModel):
    year: int = Field(ge=2000, le=2100)
    leave_type_ids: Optional[list[int]] = None
    as_of: Optional[date] = None


class CreditRunResult(BaseModel):
    year: int
    employees: int
    credit_entries: int
    carry_forward_entries: int
    days_credited: Decimal
    days_carried_forward: Decimal
    lapse_entries: int = 0
    days_lapsed: Decimal = Decimal("0")


class CreditMonthRequest(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class CreditCatchUpRequest(BaseModel):
    year: Optional[int] = Field(default=None, ge=2000, le=2100)


class MonthlyCreditResult(BaseModel):
    year: int
    month: Optional[int] = None
    months: list[str] = []
    employees: int
    credit_entries: int
    days_credited: Decimal
    lapse_entries: int
    days_lapsed: Decimal
    carry_forward_entries: int
    days_carried_forward: Decimal
    missing_joining_date: list[str] = []


class LedgerEntryRead(BaseModel):
    id: int
    employee_id: int
    leave_type_id: int
    leave_type_code: Optional[str] = None
    leave_year: int
    entry_type: str
    days: Decimal
    reference_type: Optional[str] = None
    reference_id: Optional[str] = None
    note: Optional[str] = None
    created_at: datetime


class PermissionAllowance(BaseModel):
    month: str
    monthly_minutes: int
    max_per_month: int
    max_minutes_each: int
    used_count: int
    used_minutes: int
    remaining_count: int
    remaining_minutes: int


class PendingCounts(BaseModel):
    leave: int = 0
    permissions: int = 0
    wfh: int = 0
    regularizations: int = 0


class LeaveRules(BaseModel):
    min_notice_days: int
    leave_year_start_month: int
    permission_monthly_minutes: int
    permission_max_per_month: int
    permission_max_minutes_each: int
    shift_start: time
    required_minutes: int
    notice_start_date: Optional[date] = None
    has_manager: bool
    manager_name: Optional[str] = None
    # The permission chain is configured separately from the leave chain.
    permission_has_manager: bool = False
    permission_manager_name: Optional[str] = None
    leave_chain: str = "MANAGER_HR"
    permission_chain: str = "MANAGER_HR"


class ApprovalsInbox(BaseModel):
    leave: list[LeaveApplicationRead]
    permissions: list[PermissionRequestRead]


class HolidayBrief(BaseModel):
    date: dt.date
    name: str
