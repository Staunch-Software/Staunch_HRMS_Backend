from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import get_current_user
from app.models.employee import Employee
from app.schemas.dashboard import DashboardOverview, PayrollSummaryResponse
from app.services.dashboard import build_overview

router = APIRouter(tags=["dashboard"], dependencies=[Depends(get_current_user)])


@router.get("/payroll-summary", response_model=PayrollSummaryResponse)
def payroll_summary(db: Session = Depends(get_db)):
    """No payroll runs exist yet in Phase 1 (that's Phase 2+), so
    totalPayroll/taxesWithheld/deductions are 0 by construction — this just
    guarantees Dashboard.js gets a well-shaped response instead of a 404.
    employeesPaid/averageSalary are derived from active employees' basic
    salary as a reasonable stand-in until real payroll runs exist.
    """
    active_employees = db.query(Employee).filter(Employee.status == "Active").all()
    employees_paid = len(active_employees)
    total_basic = sum(float(e.basic_salary or 0) for e in active_employees)
    average_salary = (total_basic / employees_paid) if employees_paid else 0

    return PayrollSummaryResponse(
        totalPayroll=0,
        averageSalary=average_salary,
        employeesPaid=employees_paid,
        taxesWithheld=0,
        deductions=0,
    )


@router.get("/dashboard/overview", response_model=DashboardOverview)
def dashboard_overview(
    as_of: Optional[date] = Query(None, description="Day to report on; defaults to today."),
    db: Session = Depends(get_db),
):
    """Headcount, today's attendance, leave, pending approvals and payroll
    figures for the HR dashboard. Empty data gives zeros, empty lists and a
    null payroll_summary rather than an error."""
    return build_overview(db, as_of or date.today())
