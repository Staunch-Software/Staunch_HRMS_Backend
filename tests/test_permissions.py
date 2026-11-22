from datetime import date, time, timedelta

import pytest

from app.services.leave import approved_permission_minutes_by_day
from app.services.leave.calendar import ensure_default_policy
from app.services.leave.workflow import (
    WorkflowError,
    apply_permission,
    cancel_by_employee,
    hr_approve,
    hr_reject,
    manager_decide,
    permission_allowance,
    record_permission_on_behalf,
)
from tests.leave_helpers import API, bearer, make_department, make_employee, make_user, next_working_day, token_for

TODAY = date(2026, 11, 2)  # Monday
TUE, WED, THU = date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)


@pytest.fixture()
def org(db_session):
    db = db_session
    manager = make_employee(db, "M1", "Meena")
    employee = make_employee(db, "E1", "Asha", manager=manager, department=make_department(db, "OPS", manager))
    return {
        "db": db, "manager": manager, "employee": employee,
        "mgr": make_user(db, "mgr", manager), "emp": make_user(db, "emp", employee),
        "hr": make_user(db, "hr2", role="HR"),
    }


def ask(o, day, start, end, today=TODAY):
    return apply_permission(o["db"], employee=o["employee"], user=o["emp"], day=day, from_time=start, to_time=end,
                            reason="errand", today=today)


def test_limits_per_request_count_and_minutes(org):
    o = org
    with pytest.raises(WorkflowError, match="at most 60 minutes"):
        ask(o, TUE, time(16, 0), time(17, 1))
    first = ask(o, TUE, time(9, 30), time(10, 30))
    assert first.minutes == 60 and first.status == "PENDING_MANAGER"
    ask(o, WED, time(17, 30), time(18, 0))
    with pytest.raises(WorkflowError, match="Only 2 permissions"):
        ask(o, THU, time(9, 30), time(9, 45))

    allowance = permission_allowance(o["db"], o["employee"], TUE)
    assert allowance["used_count"] == 2 and allowance["used_minutes"] == 90 and allowance["remaining_minutes"] == 30

    # Cancelled or rejected requests free the allowance.
    cancel_by_employee(o["db"], first, o["emp"])
    third = ask(o, THU, time(9, 30), time(10, 30))
    manager_decide(o["db"], third, o["mgr"], False, "no")
    assert ask(o, THU, time(9, 30), time(10, 0)).minutes == 30

    # Next month starts fresh.
    assert ask(o, date(2026, 12, 1), time(9, 30), time(10, 30)).minutes == 60


def test_monthly_minutes_limit(org):
    o = org
    policy = ensure_default_policy(o["db"])
    policy.permission_max_per_month = 5
    o["db"].commit()
    ask(o, TUE, time(9, 30), time(10, 30))
    ask(o, WED, time(9, 30), time(10, 30))
    with pytest.raises(WorkflowError, match="Monthly permission limit is 120 minutes"):
        ask(o, THU, time(9, 30), time(9, 40))


def test_date_rules(org):
    o = org
    with pytest.raises(WorkflowError, match="before, or on the day of"):
        ask(o, date(2026, 10, 30), time(9, 30), time(10, 0))
    assert ask(o, TODAY, time(9, 30), time(10, 0)).date == TODAY  # same day allowed
    with pytest.raises(WorkflowError, match="weekly off"):
        ask(o, date(2026, 11, 8), time(9, 30), time(10, 0))
    with pytest.raises(WorkflowError, match="after the start"):
        ask(o, TUE, time(10, 0), time(9, 30))
    with pytest.raises(WorkflowError, match="overlaps"):
        ask(o, TODAY, time(9, 45), time(10, 15))


def test_hr_override_and_approval_flow(org):
    o = org
    ask(o, TUE, time(9, 30), time(10, 30))
    second = ask(o, WED, time(9, 30), time(10, 30))
    with pytest.raises(WorkflowError, match="Override limits"):
        record_permission_on_behalf(o["db"], hr_user=o["hr"], employee_id=o["employee"].id, day=THU,
                                    from_time=time(9, 30), to_time=time(11, 0), reason=None, today=TODAY)
    request, warnings = record_permission_on_behalf(
        o["db"], hr_user=o["hr"], employee_id=o["employee"].id, day=date(2026, 10, 30), from_time=time(9, 30),
        to_time=time(11, 0), reason="hospital", override_limits=True, today=TODAY,
    )
    assert request.status == "APPROVED" and request.override_limits and request.recorded_by_hr
    assert any("at most 60" in w for w in warnings) and any("Backdated" in w for w in warnings)

    within, _ = record_permission_on_behalf(
        o["db"], hr_user=o["hr"], employee_id=o["manager"].id, day=TUE, from_time=time(9, 30),
        to_time=time(10, 0), reason=None, override_limits=True, today=TODAY,
    )
    assert within.override_limits is False

    manager_decide(o["db"], second, o["mgr"], True, None)
    assert second.status == "PENDING_HR"
    hr_approve(o["db"], second, o["hr"], "ok")
    minutes = approved_permission_minutes_by_day(o["db"], o["employee"].id, date(2026, 10, 1), date(2026, 11, 30))
    assert minutes == {date(2026, 10, 30): 90, WED: 60}
    with pytest.raises(WorkflowError):
        hr_reject(o["db"], second, o["hr"], None)


def test_permission_api(client, org):
    o = org
    emp, mgr = token_for(client, "emp"), token_for(client, "mgr")
    day = next_working_day(o["db"], date.today() + timedelta(days=1))
    allowance = client.get(f"{API}/me/permission-allowance", headers=bearer(emp), params={"date": day.isoformat()})
    assert allowance.status_code == 200 and allowance.json()["remaining_minutes"] == 120

    response = client.post(f"{API}/me/permissions", headers=bearer(emp), json={
        "date": day.isoformat(), "from_time": "17:30", "to_time": "18:30", "reason": "bank",
    })
    assert response.status_code == 201, response.text
    request_id = response.json()["id"]
    too_long = client.post(f"{API}/me/permissions", headers=bearer(emp), json={
        "date": day.isoformat(), "from_time": "09:30", "to_time": "11:00",
    })
    assert too_long.status_code == 400 and "60 minutes" in too_long.json()["detail"]

    inbox = client.get(f"{API}/me/approvals", headers=bearer(mgr)).json()
    assert [p["id"] for p in inbox["permissions"]] == [request_id]
    assert client.post(f"{API}/me/approvals/permission/{request_id}/approve", headers=bearer(mgr),
                       json={}).json()["status"] == "PENDING_HR"
    assert client.post(f"{API}/permissions/{request_id}/approve").json()["status"] == "APPROVED"

    recorded = client.post(f"{API}/permissions", json={
        "employee_id": o["employee"].id, "date": day.isoformat(), "from_time": "09:30", "to_time": "11:00",
        "override_limits": True,
    })
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["override_limits"] is True and recorded.json()["warnings"]
    listed = client.get(f"{API}/permissions", params={"employee_id": o["employee"].id}).json()
    assert len(listed) == 2
    mine = client.get(f"{API}/me/permissions", headers=bearer(emp)).json()
    assert {p["status"] for p in mine} == {"APPROVED"}
    assert client.post(f"{API}/permissions/{request_id}/cancel").json()["status"] == "CANCELLED"
