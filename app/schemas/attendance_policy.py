import datetime as dt
from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.models.attendance_policy import (
    DAY_OFF,
    DAY_WORK,
    MISSING_OUT_POLICIES,
    SATURDAY_OFF_HR_MONTHLY,
    SATURDAY_OFF_MODES,
    WEEKDAY_KEYS,
)


class AttendancePolicyFields(BaseModel):
    shift_start: time
    grace_until: time
    required_minutes: int = Field(ge=0, le=1440)
    weekday_pattern: dict[str, str]
    saturdays_off: list[int] = []
    saturday_off_mode: str = SATURDAY_OFF_HR_MONTHLY
    free_lates_per_month: int = Field(ge=0, le=31)
    late_deduction_days: Decimal = Field(ge=0, le=1)
    late_block_size: int = Field(default=3, ge=1, le=31)
    permission_monthly_minutes: int = Field(ge=0, le=1440)
    permission_max_per_month: int = Field(ge=0, le=31)
    permission_max_minutes_each: int = Field(ge=0, le=1440)
    permission_half_day_above_minutes: int = Field(default=120, ge=0, le=1440)
    permission_full_day_above_minutes: int = Field(default=240, ge=0, le=1440)
    missing_out_policy: str
    late_upload_hold_days: int = Field(default=2, ge=0, le=7)
    regularization_max_back_days: int = Field(default=7, ge=0, le=62)
    regularization_monthly_cap: int = Field(default=3, ge=0, le=31)
    min_notice_days: int = Field(ge=0, le=90)
    wfh_min_notice_days: int = Field(default=1, ge=0, le=90)
    leave_year_start_month: int = Field(ge=1, le=12)
    effective_from: date

    @field_validator("weekday_pattern")
    @classmethod
    def _pattern(cls, value: dict[str, str]) -> dict[str, str]:
        normalized = {}
        for key in WEEKDAY_KEYS:
            day_value = str(value.get(key, "")).upper()
            if day_value not in (DAY_WORK, DAY_OFF):
                raise ValueError(f"weekday_pattern.{key} must be WORK or OFF")
            normalized[key] = day_value
        return normalized

    @field_validator("saturdays_off")
    @classmethod
    def _saturdays(cls, value: list[int]) -> list[int]:
        if any(n < 1 or n > 5 for n in value):
            raise ValueError("saturdays_off values must be between 1 and 5")
        return sorted(set(value))

    @field_validator("saturday_off_mode")
    @classmethod
    def _saturday_mode(cls, value: str) -> str:
        value = (value or "").strip().upper()
        if value not in SATURDAY_OFF_MODES:
            raise ValueError(f"saturday_off_mode must be one of {', '.join(SATURDAY_OFF_MODES)}")
        return value

    @field_validator("missing_out_policy")
    @classmethod
    def _missing_out(cls, value: str) -> str:
        value = value.upper()
        if value not in MISSING_OUT_POLICIES:
            raise ValueError(f"missing_out_policy must be one of {', '.join(MISSING_OUT_POLICIES)}")
        return value

    @model_validator(mode="after")
    def _consistency(self):
        if self.grace_until < self.shift_start:
            raise ValueError("Grace time must be on or after the shift start time.")
        if self.permission_full_day_above_minutes < self.permission_half_day_above_minutes:
            raise ValueError("The full-day permission limit must be at least the half-day limit.")
        if self.permission_max_minutes_each > self.permission_monthly_minutes:
            raise ValueError("A single permission cannot exceed the monthly permission minutes.")
        return self


class AttendancePolicyUpdate(AttendancePolicyFields):
    pass


class AttendancePolicyOverrideCreate(AttendancePolicyFields):
    work_location_id: int


class AttendancePolicyRead(AttendancePolicyFields):
    id: Optional[int] = None
    work_location_id: Optional[int] = None
    work_location_name: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[int] = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_policy(cls, policy) -> "AttendancePolicyRead":
        read = cls.model_validate(policy)
        read.work_location_name = policy.work_location.name if policy.work_location_id and policy.work_location else None
        return read


class AttendancePolicyBundle(BaseModel):
    default: AttendancePolicyRead
    overrides: list[AttendancePolicyRead]


class HolidayBase(BaseModel):
    date: dt.date
    name: str = Field(min_length=1, max_length=150)
    work_location_id: Optional[int] = None
    is_active: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Holiday name is required")
        return value


class HolidayCreate(HolidayBase):
    pass


class HolidayUpdate(HolidayBase):
    pass


class HolidayRead(HolidayBase):
    id: int
    work_location_name: Optional[str] = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_holiday(cls, holiday) -> "HolidayRead":
        read = cls.model_validate(holiday)
        read.work_location_name = holiday.work_location.name if holiday.work_location else None
        return read


class HolidayImportError(BaseModel):
    row_number: int
    message: str


class HolidayImportResult(BaseModel):
    created: int
    skipped: int
    errors: list[HolidayImportError]


class SaturdayOffMonth(BaseModel):
    month: str
    saturdays: list[date]
    off_date: Optional[date] = None  # HR's choice for this scope
    inherited_date: Optional[date] = None  # all-locations choice (location scope only)
    assumed_date: date  # used when nothing is chosen (2nd Saturday)
    effective_date: date
    work_location_id: Optional[int] = None


class SaturdayOffYear(BaseModel):
    year: int
    work_location_id: Optional[int] = None
    saturday_off_mode: str
    months: list[SaturdayOffMonth]


class SaturdayOffUpsert(BaseModel):
    off_date: date
    work_location_id: Optional[int] = None


class AttendanceDeviceRead(BaseModel):
    id: int
    device_name: str
    display_name: Optional[str] = None
    mode: str
    is_active: bool
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None

    model_config = {"from_attributes": True}


class AttendanceDeviceUpdate(BaseModel):
    mode: str
    display_name: Optional[str] = Field(default=None, max_length=100)
    is_active: Optional[bool] = None


class AttendanceDeviceUpdateResult(BaseModel):
    device: AttendanceDeviceRead
    recomputed_from: Optional[date] = None
    recomputed_to: Optional[date] = None
    days_recomputed: int = 0
