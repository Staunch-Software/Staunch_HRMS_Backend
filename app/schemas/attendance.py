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


class AttendanceSyncStatus(BaseModel):
    today: date
    covered_through: Optional[date] = None
    expected_through: date
    stale: bool
    last_success_at: Optional[datetime] = None
    last_run_status: Optional[str] = None
    last_run_at: Optional[datetime] = None
    running: bool = False
    missing_from: Optional[date] = None
    missing_dates: list[date] = []
    sync_from: date
    sync_to: date
    max_sync_days: int


class AttendanceCatchUpResult(BaseModel):
    runs: list[AttendanceSyncRunRead]
    status: AttendanceSyncStatus


class AttendanceOverrideInfo(BaseModel):
    id: int
    kind: str
    source: str
    punch_in: Optional[datetime] = None
    punch_out: Optional[datetime] = None
    status_code: Optional[str] = None
    leave_type_code: Optional[str] = None
    half: str
    reason: Optional[str] = None
    created_by: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AttendanceOverrideRead(AttendanceOverrideInfo):
    employee_id: int
    employee_code: str
    work_date: date
    import_batch_id: Optional[str] = None
    is_active: bool
    deactivated_at: Optional[datetime] = None
    deactivated_by: Optional[str] = None


class AttendanceOverrideUpsert(BaseModel):
    kind: str
    punch_in: Optional[str] = None  # "HH:MM" (or an ISO datetime on work_date)
    punch_out: Optional[str] = None
    status_code: Optional[str] = None
    leave_type_code: Optional[str] = None
    half: Optional[str] = "FULL"
    reason: str


class AttendanceDailyRead(BaseModel):
    id: Optional[int] = None  # None for a day that exists only as an override
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
    entry_device: Optional[str] = None  # display name; None when HR's correction set that side
    exit_device: Optional[str] = None
    is_fallback_in: bool
    is_fallback_out: bool
    is_single_punch: bool
    matrix_punch_in: Optional[datetime] = None
    matrix_punch_out: Optional[datetime] = None
    override: Optional[AttendanceOverrideInfo] = None
    is_override_only: bool = False


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


class AttendanceRecomputeRequest(BaseModel):
    date_from: date
    date_to: date


class AttendanceRecomputeResult(BaseModel):
    date_from: date
    date_to: date
    days_recomputed: int
