from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel

from app.schemas.attendance import AttendanceSyncStatus


class PayrollSummaryResponse(BaseModel):
    """Shaped to match what Dashboard.js's payrollSummary state expects
    (totalPayroll/averageSalary/employeesPaid/taxesWithheld/deductions),
    camelCase via alias since the frontend consumes this JSON directly.
    """

    totalPayroll: float = 0
    averageSalary: float = 0
    employeesPaid: int = 0
    taxesWithheld: float = 0
    deductions: float = 0


# --- GET /dashboard/overview -------------------------------------------------

class DepartmentCount(BaseModel):
    department: str
    count: int


class Headcount(BaseModel):
    active: int
    by_department: list[DepartmentCount]


class AttendanceToday(BaseModel):
    date: date
    is_today: bool
    has_data: bool
    present: int
    expected: int
    percent: Optional[float]
    late: int
    late_after: str


class AttendanceTrendPoint(BaseModel):
    date: date
    present: int
    expected: int
    percent: Optional[float]


class PendingApprovalItem(BaseModel):
    kind: Literal["leave", "permission"]
    id: int
    employee_id: int
    employee_name: str
    employee_code: str
    type: str
    from_date: date
    to_date: date
    days: Optional[float]
    minutes: Optional[int]
    status: str
    step: str
    applied_at: datetime


class PendingApprovals(BaseModel):
    total: int
    leave: int
    permission: int
    pending_manager: int
    pending_hr: int
    items: list[PendingApprovalItem]


class PayrollMonthSummary(BaseModel):
    month: str
    label: str
    gross: float
    deductions: float
    net: float
    tds: float
    employees_paid: int
    runs: int
    status_counts: dict[str, int]


class PayrollTrendPoint(BaseModel):
    month: str
    label: str
    gross: float
    deductions: float
    net: float
    employees: int


class RecentRun(BaseModel):
    id: int
    pay_group: str
    period_start: date
    period_end: date
    period_label: str
    pay_date: date
    status: str
    source: str
    employees: int
    net: float


class DashboardOverview(BaseModel):
    as_of: date
    headcount: Headcount
    attendance_today: AttendanceToday
    on_leave_today: int
    pending_approvals: PendingApprovals
    payroll_summary: Optional[PayrollMonthSummary]
    payroll_trend: list[PayrollTrendPoint]
    attendance_trend: list[AttendanceTrendPoint]
    recent_runs: list[RecentRun]
    attendance_sync: Optional[AttendanceSyncStatus] = None
