"""HR dashboard overview: headcount, attendance, leave, approvals, payroll."""
from datetime import date

from sqlalchemy.orm import Session

from app.services.attendance.coverage import coverage_status
from app.services.dashboard.attendance import attendance_overview
from app.services.dashboard.payroll import payroll_overview
from app.services.dashboard.people import headcount, on_leave_count, pending_approvals


def build_overview(db: Session, as_of: date) -> dict:
    attendance_today, attendance_trend = attendance_overview(db, as_of)
    payroll_summary, payroll_trend, recent_runs = payroll_overview(db, as_of)
    return {
        "as_of": as_of,
        "headcount": headcount(db),
        "attendance_today": attendance_today,
        "on_leave_today": on_leave_count(db, as_of),
        "pending_approvals": pending_approvals(db),
        "payroll_summary": payroll_summary,
        "payroll_trend": payroll_trend,
        "attendance_trend": attendance_trend,
        "recent_runs": recent_runs,
        "attendance_sync": coverage_status(db).to_json(),
    }
