"""GET /dashboard/overview: headcount, attendance, leave, approvals, payroll."""
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.models.attendance import AttendanceDaily, AttendanceOverride
from app.models.leave import LeaveApplication, PermissionRequest
from app.models.org import Department
from app.models.pay_group import PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from tests.leave_helpers import bearer, make_employee, make_user, seed_types, token_for

URL = "/api/v1/dashboard/overview"
WED = date(2026, 11, 18)  # a working day under the default policy


def overview(client, as_of=WED):
    response = client.get(URL, params={"as_of": as_of.isoformat()})
    assert response.status_code == 200, response.text
    return response.json()


def punch(db, emp, day, hhmm):
    at = datetime.combine(day, time.fromisoformat(hhmm))
    db.add(AttendanceDaily(employee_code=emp.employee_code, employee_id=emp.id, work_date=day,
                           punch_in=at, punch_in_source="IN", punch_count=1))
    db.commit()


def leave(db, emp, leave_type, start, end, status, created=None):
    row = LeaveApplication(employee_id=emp.id, leave_type_id=leave_type.id, from_date=start, to_date=end,
                           days=Decimal((end - start).days + 1), status=status)
    if created:
        row.created_at = created
    db.add(row)
    db.commit()
    return row


def test_empty_database_gives_zeros(client):
    data = overview(client)
    assert data["headcount"] == {"active": 0, "by_department": []}
    assert data["on_leave_today"] == 0
    assert data["pending_approvals"]["total"] == 0 and data["pending_approvals"]["items"] == []
    assert data["payroll_summary"] is None
    assert data["recent_runs"] == []
    assert [p["month"] for p in data["payroll_trend"]] == [
        "2026-06", "2026-07", "2026-08", "2026-09", "2026-10", "2026-11"]
    assert all(p["net"] == 0 for p in data["payroll_trend"])
    today = data["attendance_today"]
    assert today["date"] == WED.isoformat() and today["has_data"] is False
    assert today["present"] == 0 and today["expected"] == 0 and today["percent"] is None
    assert len(data["attendance_trend"]) == 30
    assert data["attendance_trend"][-1]["date"] == WED.isoformat()


def test_headcount_top_departments_and_others(client, db_session):
    for i in range(8):
        dept = Department(name=f"Dept {i}", code=f"D{i}")
        db_session.add(dept)
        db_session.commit()
        for j in range(8 - i):
            emp = make_employee(db_session, f"E{i}{j}")
            emp.department_id = dept.id
    make_employee(db_session, "NODEPT")
    make_employee(db_session, "GONE", status="Inactive")
    db_session.commit()

    headcount = overview(client)["headcount"]
    assert headcount["active"] == 37
    names = [row["department"] for row in headcount["by_department"]]
    assert names == ["Dept 0", "Dept 1", "Dept 2", "Dept 3", "Dept 4", "Dept 5", "Others"]
    assert headcount["by_department"][-1]["count"] == 2 + 1 + 1  # Dept 6, Dept 7, Unassigned


def test_attendance_today_late_and_leave(client, db_session):
    types = seed_types(db_session)
    on_time, late, marked, on_leave, missing = (
        make_employee(db_session, code) for code in ("A1", "A2", "A3", "A4", "A5"))
    punch(db_session, on_time, WED, "09:20")
    punch(db_session, late, WED, "09:50")
    db_session.add(AttendanceOverride(employee_id=marked.id, employee_code=marked.employee_code, work_date=WED,
                                      kind="STATUS", status_code="P", source="MANUAL", is_active=True))
    db_session.commit()
    leave(db_session, on_leave, types["CL"], WED, WED, "APPROVED")
    punch(db_session, on_time, WED - timedelta(days=1), "09:25")

    data = overview(client)
    today = data["attendance_today"]
    assert today["is_today"] is True and today["has_data"] is True
    assert today["present"] == 3 and today["expected"] == 4 and today["percent"] == 75.0
    assert today["late"] == 1 and today["late_after"] == "09:35"
    assert data["on_leave_today"] == 1
    trend = {p["date"]: p for p in data["attendance_trend"]}
    assert trend[(WED - timedelta(days=1)).isoformat()]["present"] == 1
    assert trend[(WED - timedelta(days=1)).isoformat()]["expected"] == 5
    sunday = date(2026, 11, 15)
    assert trend[sunday.isoformat()]["expected"] == 0 and trend[sunday.isoformat()]["percent"] is None


def test_attendance_falls_back_to_latest_day_with_data(client, db_session):
    emp = make_employee(db_session, "B1")
    tue = WED - timedelta(days=1)
    punch(db_session, emp, tue, "09:00")

    today = overview(client)["attendance_today"]
    assert today["date"] == tue.isoformat()
    assert today["is_today"] is False and today["has_data"] is True
    assert today["present"] == 1 and today["percent"] == 100.0


def test_pending_approvals_counts_and_latest_items(client, db_session):
    types = seed_types(db_session)
    emp = make_employee(db_session, "C1", first="Asha")
    base = datetime(2026, 11, 10, 9, 0)
    leave(db_session, emp, types["CL"], WED, WED, "PENDING_MANAGER", created=base)
    leave(db_session, emp, types["SL"], WED, WED + timedelta(days=1), "PENDING_HR", created=base + timedelta(hours=1))
    leave(db_session, emp, types["EL"], WED, WED, "APPROVED", created=base + timedelta(hours=2))
    db_session.add(PermissionRequest(employee_id=emp.id, date=WED, from_time=time(10), to_time=time(11),
                                     minutes=60, status="PENDING_HR", created_at=base + timedelta(hours=3)))
    db_session.commit()

    pending = overview(client)["pending_approvals"]
    assert pending["total"] == 3 and pending["leave"] == 2 and pending["permission"] == 1
    assert pending["pending_manager"] == 1 and pending["pending_hr"] == 2
    kinds = [(item["kind"], item["type"], item["step"]) for item in pending["items"]]
    assert kinds == [("permission", "Permission", "HR"), ("leave", "Sick Leave", "HR"),
                     ("leave", "Casual Leave", "Manager")]
    assert pending["items"][0]["employee_name"] == "Asha" and pending["items"][0]["minutes"] == 60
    assert pending["items"][1]["days"] == 2.0


def _run(db, schedule, start, end, status, group=None, lines=(), net=None):
    run = PayrollRun(pay_schedule_id=schedule.id, pay_group_id=group.id if group else None, period_start=start,
                     period_end=end, pay_date=end, status=status, total_net=net, employee_count=len(lines))
    db.add(run)
    db.commit()
    for emp, gross, ded, tds, line_status in lines:
        breakdown = [{"component_code": "BASIC", "component_type": "Earning", "resolved_amount": gross}]
        if tds:
            breakdown.append({"component_code": "TDS", "component_type": "Deduction", "resolved_amount": tds})
        db.add(PayrollRunLineItem(payroll_run_id=run.id, employee_id=emp.id, employee_snapshot={},
                                  gross_earnings=gross, total_deductions=ded, net_pay=gross - ded,
                                  status=line_status, computation_breakdown=breakdown))
    db.commit()
    return run


def test_payroll_summary_trend_and_recent_runs(client, db_session):
    schedule = PaySchedule(schedule_name="Monthly - All", pay_frequency="Monthly",
                           payment_day_rule={"type": "LastDayOfMonth"}, holiday_adjustment="Before", is_active=True)
    group = PayGroup(code="HO", name="Head Office", calc_model="STAFF_MONTHLY", params={})
    db_session.add_all([schedule, group])
    db_session.commit()
    a, b, c = (make_employee(db_session, code) for code in ("P1", "P2", "P3"))

    _run(db_session, schedule, date(2026, 9, 1), date(2026, 9, 30), "Approved",
         lines=[(a, 30000, 2000, 0, "Included")], net=28000)
    _run(db_session, schedule, date(2026, 10, 1), date(2026, 10, 31), "Paid", group=group, net=73000,
         lines=[(a, 30000, 2000, 500, "Included"), (b, 50000, 5000, 1500, "Included"),
                (c, 99999, 0, 0, "Excluded")])
    _run(db_session, schedule, date(2026, 10, 1), date(2026, 10, 31), "Draft",
         lines=[(c, 10000, 0, 0, "Included")])
    cancelled = _run(db_session, schedule, date(2026, 11, 1), date(2026, 11, 30), "Cancelled",
                     lines=[(a, 1, 0, 0, "Included")])

    data = overview(client)
    summary = data["payroll_summary"]
    assert summary["month"] == "2026-10" and summary["label"] == "Oct 2026"
    assert summary["gross"] == 80000 and summary["deductions"] == 7000 and summary["net"] == 73000
    assert summary["tds"] == 2000 and summary["employees_paid"] == 2
    assert summary["runs"] == 2 and summary["status_counts"] == {"Paid": 1, "Draft": 1}

    trend = data["payroll_trend"]
    assert [p["month"] for p in trend] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert trend[-2]["net"] == 28000 and trend[-2]["employees"] == 1
    assert trend[-1]["gross"] == 80000

    recent = data["recent_runs"]
    assert recent[0]["id"] == cancelled.id and recent[0]["status"] == "Cancelled"
    assert recent[2]["pay_group"] == "Head Office" and recent[2]["period_label"] == "Oct 2026"
    assert recent[3]["pay_group"] == "Monthly - All" and recent[3]["employees"] == 1


def test_overview_is_hr_only(client, db_session):
    employee = make_employee(db_session, "Z1")
    make_user(db_session, "z1", employee=employee)
    response = client.get(URL, headers=bearer(token_for(client, "z1")))
    assert response.status_code == 403
