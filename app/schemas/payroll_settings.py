from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

NetRounding = Literal["NONE", "ROUND", "ROUNDUP", "ROUNDDOWN"]
LineRounding = Literal["NONE", "ROUND", "ROUNDUP", "ROUNDDOWN"]
TdsRounding = Literal["NEAREST_RUPEE", "NEAREST_10"]
DayStep = Literal["0.5", "0.25", "NONE"]
Template = Literal["CLASSIC", "MODERN", "COMPACT"]
PdfPassword = Literal["NONE", "PAN", "DOB"]
ReleaseOn = Literal["APPROVED", "PAID"]


class RoundingSettingsFields(BaseModel):
    default_net_rounding: NetRounding = "ROUND"
    default_line_rounding: LineRounding = "ROUND"
    default_line_decimals: int = Field(0, ge=0, le=6)
    tds_rounding: TdsRounding = "NEAREST_RUPEE"
    attendance_day_step: DayStep = "0.5"


class PayGroupRounding(BaseModel):
    id: int
    code: str
    name: str
    is_active: bool
    calc_model: str
    definition_version: int
    net_rounding: str
    editable: bool


class RoundingSettingsRead(RoundingSettingsFields):
    model_config = ConfigDict(from_attributes=True)

    updated_at: Optional[datetime] = None
    pay_groups: list[PayGroupRounding] = []


class ApplyNetRoundingRequest(BaseModel):
    net_rounding: Optional[NetRounding] = None


class ApplyNetRoundingResult(BaseModel):
    net_rounding: str
    updated: list[dict]
    unchanged: list[str]
    skipped: list[str]


class PayslipSettingsFields(BaseModel):
    company_name: str = Field(..., min_length=1, max_length=200)
    company_address: Optional[str] = None
    company_cin: Optional[str] = Field(None, max_length=30)
    company_gstin: Optional[str] = Field(None, max_length=20)
    template: Template = "CLASSIC"
    show_pan: bool = True
    show_uan: bool = True
    show_bank: bool = True
    show_attendance: bool = True
    show_leave_balance: bool = True
    show_ytd: bool = True
    show_employer_contrib: bool = False
    footer_note: Optional[str] = None
    pdf_password: PdfPassword = "NONE"
    release_on: ReleaseOn = "APPROVED"

    @field_validator("company_name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("company name is required")
        return value

    @field_validator("company_address", "company_cin", "company_gstin", "footer_note")
    @classmethod
    def _blank_to_none(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None


class PayslipSettingsRead(PayslipSettingsFields):
    model_config = ConfigDict(from_attributes=True)

    has_custom_logo: bool = False
    updated_at: Optional[datetime] = None


class MyPayslipRow(BaseModel):
    run_id: int
    period_start: date
    period_end: date
    pay_date: date
    month_label: str
    status: str
    gross_earnings: float
    total_deductions: float
    net_pay: float
