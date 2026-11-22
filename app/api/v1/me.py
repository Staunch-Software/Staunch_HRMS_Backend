"""Employee self-service endpoints. Everything here is scoped to the
caller's own employee record (AdminUser.employee_id); leave/attendance
endpoints are added to this router by the leave module."""
from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_employee_user
from app.models.employee import Employee
from app.models.user import AdminUser
from app.schemas.attendance_monthly import MyAttendanceMonth
from app.schemas.reporting import MyProfile
from app.services.attendance import monthly
from app.services.reporting import employee_full_name, has_team

router = APIRouter(prefix="/me", tags=["self-service"], dependencies=[Depends(require_employee_user)])


def get_current_employee(
    user: AdminUser = Depends(require_employee_user), db: Session = Depends(get_db)
) -> Employee:
    employee = db.get(Employee, user.employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee record not found")
    return employee


@router.get("/profile", response_model=MyProfile)
def read_my_profile(employee: Employee = Depends(get_current_employee), db: Session = Depends(get_db)):
    manager = employee.reporting_manager
    return MyProfile(
        employee_id=employee.id,
        employee_code=employee.employee_code,
        name=employee_full_name(employee),
        first_name=employee.first_name,
        last_name=employee.last_name,
        department=employee.department.name if employee.department else None,
        designation=employee.designation.name if employee.designation else None,
        work_location=employee.work_location.name if employee.work_location else None,
        date_of_joining=employee.date_of_joining,
        reporting_manager_id=employee.reporting_manager_id,
        reporting_manager_name=employee_full_name(manager),
        reporting_manager_code=manager.employee_code if manager else None,
        work_email=employee.work_email,
        personal_email=employee.personal_email,
        mobile_number=employee.mobile_number,
        status=employee.status,
        has_reports=has_team(db, employee.id),
    )


@router.get("/attendance", response_model=MyAttendanceMonth)
def read_my_attendance(
    month: Optional[str] = Query(None, description="YYYY-MM (default: this month)"),
    employee: Employee = Depends(get_current_employee),
    db: Session = Depends(get_db),
):
    """The caller's month resolved now under the attendance policy (read-only)."""
    try:
        result = monthly.resolve_month(db, employee, month or date.today().strftime("%Y-%m"))
    except monthly.MonthlyError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return MyAttendanceMonth(
        month=result.month, date_from=result.date_from, date_to=result.date_to,
        employee_code=employee.employee_code, employee_name=employee_full_name(employee),
        totals=result.totals, days=result.days_json(), warnings=result.warnings,
    )
