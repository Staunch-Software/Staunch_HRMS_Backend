from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel


class AttendanceSyncRequest(BaseModel):
    date_from: Optional[date] = None
    date_to: Optional[date] = None


class AttendanceSyncRunRead(BaseModel):
    id: int
    triggered_by: str
    date_from: date
    date_to: date
    started_at: datetime
    finished_at: Optional[datetime] = None
    status: str
    records_fetched: int
    records_new: int
    days_recomputed: int
    error_message: Optional[str] = None

    model_config = {"from_attributes": True}


class AttendanceDailyRead(BaseModel):
    id: int
    employee_code: str
    employee_id: Optional[int] = None
    is_matched: bool
    employee_name: Optional[str] = None
    department: Optional[str] = None
    username: Optional[str] = None
    work_date: date
    punch_in: Optional[datetime] = None
    punch_in_source: Optional[str] = None
    punch_out: Optional[datetime] = None
    punch_out_source: Optional[str] = None
    worked_minutes: Optional[int] = None
    punch_count: int
    is_fallback_in: bool
    is_fallback_out: bool
    is_single_punch: bool


class AttendancePunchRead(BaseModel):
    id: int
    employee_code: str
    raw_userid: str
    username: Optional[str] = None
    event_datetime: datetime
    event_date: date
    entry_exit_type: str
    direction: str
    device_name: str
    received_at: Optional[datetime] = None

    model_config = {"from_attributes": True}


class AttendanceMonthlySummaryRow(BaseModel):
    employee_code: str
    employee_id: Optional[int] = None
    is_matched: bool
    employee_name: Optional[str] = None
    department: Optional[str] = None
    username: Optional[str] = None
    days_present: int
    days_with_fallback_out: int
    total_worked_minutes: int


class AttendanceMonthlySummary(BaseModel):
    month: str
    date_from: date
    date_to: date
    employees: list[AttendanceMonthlySummaryRow]


class AttendanceRelinkResult(BaseModel):
    codes_relinked: int
