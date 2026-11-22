"""Monthly attendance processing API shapes. Day counts inside `totals` and
`days` are strings ("0.5"), as stored."""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, Field


class MonthlyProcessRequest(BaseModel):
    month: str = Field(description="YYYY-MM")
    pay_group_id: int
    # Re-process only these employees (e.g. after correcting a day).
    employee_ids: Optional[list[int]] = None


class MonthlySendRequest(BaseModel):
    month: str = Field(description="YYYY-MM")
    pay_group_id: int
    employee_ids: Optional[list[int]] = None


class MonthlyResultRow(BaseModel):
    employee_id: int
    employee_code: Optional[str] = None
    employee_name: Optional[str] = None
    department: Optional[str] = None
    status: str
    changed_since_sent: bool = False
    lop_days: Decimal
    present_days: Decimal
    paid_leave_days: Decimal
    totals: dict[str, Any]
    warnings: list[str] = []
    processed_at: Optional[datetime] = None
    processed_by: Optional[str] = None
    sent_at: Optional[datetime] = None
    sent_by: Optional[str] = None


class MonthlyResultList(BaseModel):
    month: str
    date_from: date
    date_to: date
    pay_group_id: int
    pay_group_code: str
    pay_group_name: str
    calc_model: str
    attendance_source: str
    results: list[MonthlyResultRow]
    warnings: list[str] = []
    saturday_off_warnings: list[str] = []
    employees_in_scope: int = 0
    not_processed: list[str] = []
    no_data_employees: list[str] = []
    missing_punch_days: int = 0
    unmatched_punch_codes: list[str] = []
    total_lop: Decimal = Decimal("0")
    uncovered_sync_dates: list[date] = []
    unsettled_from: Optional[date] = None
    late_upload_hold_days: int = 0


class MonthlyProcessResult(MonthlyResultList):
    processed: int = 0
    created: int = 0
    removed: int = 0


class MonthlyEmployeeDetail(BaseModel):
    employee_id: int
    employee_code: Optional[str] = None
    employee_name: Optional[str] = None
    month: str
    date_from: date
    date_to: date
    status: str  # PROCESSED | SENT | NOT_PROCESSED
    changed_since_sent: bool = False
    pay_group_id: Optional[int] = None
    totals: dict[str, Any]
    days: list[dict[str, Any]]
    warnings: list[str] = []
    processed_at: Optional[datetime] = None
    processed_by: Optional[str] = None
    sent_at: Optional[datetime] = None
    sent_by: Optional[str] = None


class MonthlySendResult(BaseModel):
    month: str
    pay_group_id: int
    sent: int
    total_lop: Decimal
    inputs_created: int
    inputs_updated: int
    ledger_rows: int


class MyAttendanceMonth(BaseModel):
    month: str
    date_from: date
    date_to: date
    employee_code: str
    employee_name: Optional[str] = None
    totals: dict[str, Any]
    days: list[dict[str, Any]]
    warnings: list[str] = []
