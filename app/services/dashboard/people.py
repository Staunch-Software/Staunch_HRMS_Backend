"""Dashboard people figures: headcount, who is on leave, pending approvals."""
from collections import defaultdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import (
    HALF_FULL,
    PENDING_STATUSES,
    STATUS_APPROVED,
    STATUS_PENDING_MANAGER,
    LeaveApplication,
    LeaveType,
    PermissionRequest,
)
from app.models.org import Department

ACTIVE = "Active"
TOP_DEPARTMENTS = 6


def full_name(first, middle, last) -> str:
    return " ".join(part for part in (first, middle, last) if part)


def active_employees(db: Session) -> list:
    """Explicit columns only, so this keeps working while newer Employee
    columns are still unmigrated in a database."""
    stmt = select(
        Employee.id, Employee.work_location_id, Employee.date_of_joining
    ).where(Employee.status == ACTIVE)
    return list(db.execute(stmt))


def headcount(db: Session) -> dict:
    stmt = (
        select(Department.name, func.count(Employee.id))
        .select_from(Employee)
        .outerjoin(Department, Department.id == Employee.department_id)
        .where(Employee.status == ACTIVE)
        .group_by(Department.name)
    )
    counts = sorted(((name or "Unassigned", n) for name, n in db.execute(stmt)), key=lambda r: (-r[1], r[0]))
    by_department = [{"department": name, "count": n} for name, n in counts[:TOP_DEPARTMENTS]]
    rest = sum(n for _, n in counts[TOP_DEPARTMENTS:])
    if rest:
        by_department.append({"department": "Others", "count": rest})
    return {"active": sum(n for _, n in counts), "by_department": by_department}


def _approved_leaves(db: Session, start: date, end: date) -> list[LeaveApplication]:
    stmt = select(LeaveApplication).where(
        LeaveApplication.status == STATUS_APPROVED,
        LeaveApplication.from_date <= end,
        LeaveApplication.to_date >= start,
    )
    return list(db.scalars(stmt))


def on_leave_count(db: Session, day: date) -> int:
    """Employees with approved leave covering `day` (half days included)."""
    return len({leave.employee_id for leave in _approved_leaves(db, day, day)})


def full_day_leave_ids(db: Session, start: date, end: date) -> dict[date, set[int]]:
    """{day: employee ids on approved leave for the whole of that day}; a
    half-day start or end leaves the employee expected for the other half."""
    result: dict[date, set[int]] = defaultdict(set)
    for leave in _approved_leaves(db, start, end):
        day = max(leave.from_date, start)
        while day <= min(leave.to_date, end):
            half_start = day == leave.from_date and (leave.from_half or HALF_FULL) != HALF_FULL
            half_end = day == leave.to_date and (leave.to_half or HALF_FULL) != HALF_FULL
            if not (half_start or half_end):
                result[day].add(leave.employee_id)
            day += timedelta(days=1)
    return result


def _step(status: str) -> str:
    return "Manager" if status == STATUS_PENDING_MANAGER else "HR"


def _status_counts(db: Session, model) -> dict[str, int]:
    stmt = select(model.status, func.count()).where(model.status.in_(PENDING_STATUSES)).group_by(model.status)
    return {status: n for status, n in db.execute(stmt)}


def _employee_fields(employee: Employee) -> dict:
    return {
        "employee_id": employee.id,
        "employee_name": full_name(employee.first_name, employee.middle_name, employee.last_name),
        "employee_code": employee.employee_code,
    }


def pending_approvals(db: Session, limit: int = 5) -> dict:
    leave_counts = _status_counts(db, LeaveApplication)
    permission_counts = _status_counts(db, PermissionRequest)

    leave_rows = db.execute(
        select(LeaveApplication, Employee, LeaveType)
        .join(Employee, Employee.id == LeaveApplication.employee_id)
        .join(LeaveType, LeaveType.id == LeaveApplication.leave_type_id)
        .where(LeaveApplication.status.in_(PENDING_STATUSES))
        .order_by(LeaveApplication.created_at.desc(), LeaveApplication.id.desc())
        .limit(limit)
    )
    items = [
        {
            "kind": "leave", "id": leave.id, **_employee_fields(employee),
            "type": leave_type.name, "from_date": leave.from_date, "to_date": leave.to_date,
            "days": float(leave.days or 0), "minutes": None,
            "status": leave.status, "step": _step(leave.status), "applied_at": leave.created_at,
        }
        for leave, employee, leave_type in leave_rows
    ]
    permission_rows = db.execute(
        select(PermissionRequest, Employee)
        .join(Employee, Employee.id == PermissionRequest.employee_id)
        .where(PermissionRequest.status.in_(PENDING_STATUSES))
        .order_by(PermissionRequest.created_at.desc(), PermissionRequest.id.desc())
        .limit(limit)
    )
    items += [
        {
            "kind": "permission", "id": req.id, **_employee_fields(employee),
            "type": "Permission", "from_date": req.date, "to_date": req.date,
            "days": None, "minutes": req.minutes,
            "status": req.status, "step": _step(req.status), "applied_at": req.created_at,
        }
        for req, employee in permission_rows
    ]
    items.sort(key=lambda item: (item["applied_at"].replace(tzinfo=None), item["id"]), reverse=True)

    leave_total = sum(leave_counts.values())
    permission_total = sum(permission_counts.values())
    return {
        "total": leave_total + permission_total,
        "leave": leave_total,
        "permission": permission_total,
        "pending_manager": leave_counts.get(STATUS_PENDING_MANAGER, 0) + permission_counts.get(STATUS_PENDING_MANAGER, 0),
        "pending_hr": (leave_total + permission_total)
        - leave_counts.get(STATUS_PENDING_MANAGER, 0) - permission_counts.get(STATUS_PENDING_MANAGER, 0),
        "items": items[:limit],
    }
