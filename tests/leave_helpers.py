"""Shared builders for the leave / permission / attendance-settings tests."""
from datetime import date, timedelta
from decimal import Decimal

from app.core.security import hash_password
from app.models.employee import Employee
from app.models.leave import ENTRY_CREDIT, LeaveLedger, LeaveType
from app.models.org import Department, WorkLocation
from app.models.user import AdminUser
from app.services.leave.calendar import default_policy, is_weekly_off

API = "/api/v1"
PASSWORD = "Passw0rd!"


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make_location(db, name="Chennai", state_code="TN"):
    location = WorkLocation(name=name, state_code=state_code, status="Active")
    db.add(location)
    db.commit()
    return location


def make_department(db, code="OPS", manager=None, name=None):
    department = Department(name=name or code.title(), code=code, status="Active",
                            manager_employee_id=manager.id if manager else None)
    db.add(department)
    db.commit()
    return department


def make_employee(db, code, first="Emp", manager=None, status="Active", location=None, joined=None, notice=None,
                  department=None):
    employee = Employee(
        employee_code=code, first_name=first, status=status,
        reporting_manager_id=manager.id if manager else None,
        department_id=department.id if department else None,
        work_location_id=location.id if location else None,
        date_of_joining=joined, notice_start_date=notice,
    )
    db.add(employee)
    db.commit()
    return employee


def make_user(db, username, employee=None, role="EMPLOYEE", active=True):
    user = AdminUser(
        username=username, hashed_password=hash_password(PASSWORD), is_active=active, role=role,
        employee_id=employee.id if employee else None, must_change_password=False,
    )
    db.add(user)
    db.commit()
    return user


def token_for(client, username):
    response = client.post(f"{API}/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def seed_types(db, cl=12, sl=6, el=15):
    rows = [
        LeaveType(code="CL", name="Casual Leave", annual_entitlement=Decimal(cl), sort_order=1),
        LeaveType(code="SL", name="Sick Leave", annual_entitlement=Decimal(sl), sort_order=2),
        LeaveType(code="EL", name="Earned Leave", annual_entitlement=Decimal(el), sandwich_rule=False,
                  carry_forward=True, carry_forward_limit=Decimal("30"), sort_order=3),
        LeaveType(code="LOP", name="Loss of Pay", is_paid=False, allow_negative_balance=True, sort_order=4),
    ]
    for row in rows:
        row.annual_entitlement = row.annual_entitlement if row.annual_entitlement is not None else Decimal("0")
        for field, value in (("credit_mode", "YEARLY_UPFRONT"), ("pro_rata_for_joiners", True),
                             ("carry_forward", False), ("is_paid", True), ("allow_half_day", True),
                             ("sandwich_rule", True), ("allowed_during_notice", False),
                             ("allow_negative_balance", False), ("is_active", True)):
            if getattr(row, field) is None:
                setattr(row, field, value)
        db.add(row)
    db.commit()
    return {row.code: row for row in rows}


def credit(db, employee, leave_type, days, year):
    db.add(LeaveLedger(employee_id=employee.id, leave_type_id=leave_type.id, leave_year=year,
                       entry_type=ENTRY_CREDIT, days=Decimal(str(days)), note="test credit"))
    db.commit()


def next_working_day(db, start: date) -> date:
    policy = default_policy(db)
    day = start
    while is_weekly_off(policy, day):
        day += timedelta(days=1)
    return day
