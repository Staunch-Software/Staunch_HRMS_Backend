from datetime import date
from typing import Optional

from pydantic import BaseModel

from app.services.reporting import employee_full_name


class EmployeeBrief(BaseModel):
    """Non-sensitive employee summary (no salary, IDs or bank details)."""

    id: int
    employee_code: str
    name: Optional[str] = None
    department: Optional[str] = None
    designation: Optional[str] = None
    work_location: Optional[str] = None
    status: Optional[str] = None

    @classmethod
    def from_employee(cls, employee) -> "EmployeeBrief":
        return cls(
            id=employee.id,
            employee_code=employee.employee_code,
            name=employee_full_name(employee),
            department=employee.department.name if employee.department else None,
            designation=employee.designation.name if employee.designation else None,
            work_location=employee.work_location.name if employee.work_location else None,
            status=employee.status,
        )


class MyProfile(BaseModel):
    employee_id: int
    employee_code: str
    name: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    department: Optional[str] = None
    designation: Optional[str] = None
    work_location: Optional[str] = None
    date_of_joining: Optional[date] = None
    reporting_manager_id: Optional[int] = None
    reporting_manager_name: Optional[str] = None
    reporting_manager_code: Optional[str] = None
    work_email: Optional[str] = None
    personal_email: Optional[str] = None
    mobile_number: Optional[str] = None
    status: Optional[str] = None
    has_reports: bool = False
