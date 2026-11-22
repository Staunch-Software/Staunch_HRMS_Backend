"""Work-from-home requests: workflow, rules, notifications and the API."""
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.notification import Notification
from app.services.leave.calendar import ensure_default_policy
from app.services.leave.wfh import apply_wfh, record_wfh_on_behalf
from app.services.leave.workflow import (
    WorkflowError,
    apply_leave,
    cancel_by_employee,
    cancel_by_hr,
    hr_approve,
    manager_decide,
)
from tests.leave_helpers import (
    API,
    bearer,
    credit,
    make_department,
    make_employee,
    make_user,
    next_working_day,
    seed_types,
    token_for,
)

TODAY = date(2026, 11, 2)  # Monday
SAT7, SUN8, MON9, TUE10, THU12 = (date(2026, 11, n) for n in (7, 8, 9, 10, 12))


@pytest.fixture()
def org(db_session):
    db = db_session
    types = seed_types(db)
    head = make_employee(db, "M1", "Meena")
    employee = make_employee(db, "E1", "Asha", department=make_department(db, "OPS", head))
    credit(db, employee, types["CL"], 12, 2026)
    users = {"head": make_user(db, "head", head), "employee": make_user(db, "emp", employee),
             "hr": make_user(db, "hr2", role="HR")}
    return {"db": db, "types": types, "head": head, "employee": employee, "users": users}


def wfh(o, start, end=None, half="FULL", today=TODAY):
    return apply_wfh(o["db"], employee=o["employee"], user=o["users"]["employee"], from_date=start,
                     to_date=end or start, half=half, reason="internet at home", today=today)


def notes(o, who):
    return o["db"].query(Notification).filter(Notification.user_id == o["users"][who].id).order_by(
        Notification.id).all()


def test_manager_then_hr_flow_and_notifications(org):
    o = org
    request = wfh(o, SAT7, MON9)  # Sunday is off: 2 working days
    assert request.status == "PENDING_MANAGER" and request.days == Decimal("2")
    assert request.manager_id == o["head"].id
    [note] = notes(o, "head")
    assert note.type == "WFH_SUBMITTED" and note.link == "/portal/team-approvals"
    assert "Work from home" in note.message

    manager_decide(o["db"], request, o["users"]["head"], True, "ok")
    assert request.status == "PENDING_HR"
    [hr_note] = notes(o, "hr")
    assert hr_note.type == "WFH_PENDING_HR" and hr_note.link == "/app/leave/wfh"

    hr_approve(o["db"], request, o["users"]["hr"], None)
    assert request.status == "APPROVED"
    assert [n.type for n in notes(o, "employee")] == ["WFH_MANAGER_APPROVED", "WFH_APPROVED"]
    assert all(n.link == "/portal/requests" for n in notes(o, "employee"))


def test_half_day_and_date_rules(org):
    o = org
    assert wfh(o, TUE10, half="FIRST_HALF").days == Decimal("0.5")
    with pytest.raises(WorkflowError, match="single day"):
        wfh(o, MON9, TUE10, half="FIRST_HALF")
    with pytest.raises(WorkflowError, match="no working days"):
        wfh(o, SUN8)
    with pytest.raises(WorkflowError, match="at least 1 day"):
        wfh(o, TODAY)
    with pytest.raises(WorkflowError, match="Backdated"):
        wfh(o, date(2026, 10, 30))
    ensure_default_policy(o["db"]).wfh_min_notice_days = 0
    o["db"].commit()
    assert wfh(o, TODAY).days == Decimal("1")


def test_overlaps_with_wfh_and_leave(org):
    o = org
    wfh(o, MON9)
    with pytest.raises(WorkflowError, match="existing WFH"):
        wfh(o, SAT7, MON9)
    apply_leave(o["db"], employee=o["employee"], user=o["users"]["employee"], leave_type_id=o["types"]["CL"].id,
                from_date=TUE10, from_half="FULL", to_date=TUE10, to_half="FULL", reason=None, today=TODAY)
    with pytest.raises(WorkflowError, match="leave request"):
        wfh(o, TUE10)
    apply_leave(o["db"], employee=o["employee"], user=o["users"]["employee"], leave_type_id=o["types"]["CL"].id,
                from_date=THU12, from_half="FIRST_HALF", to_date=THU12, to_half="FIRST_HALF", reason=None,
                today=TODAY)
    with pytest.raises(WorkflowError, match="leave request"):
        wfh(o, THU12, half="FIRST_HALF")
    assert wfh(o, THU12, half="SECOND_HALF").days == Decimal("0.5")


def test_hr_record_backdated_and_cancel_rules(org):
    o = org
    request, warnings = record_wfh_on_behalf(
        o["db"], hr_user=o["users"]["hr"], employee_id=o["employee"].id, from_date=date(2026, 10, 30),
        to_date=date(2026, 10, 30), half="FULL", reason=None, comment="approved on call", today=TODAY,
    )
    assert request.status == "APPROVED" and request.recorded_by_hr and request.manager_id == o["head"].id
    assert any("Backdated" in w for w in warnings)
    assert [n.type for n in notes(o, "employee")] == ["WFH_RECORDED"]

    with pytest.raises(WorkflowError) as exc:
        cancel_by_employee(o["db"], request, o["users"]["employee"])
    assert exc.value.status_code == 409
    cancel_by_hr(o["db"], request, o["users"]["hr"], "wrong date")
    assert request.status == "CANCELLED"

    pending = wfh(o, MON9)
    cancel_by_employee(o["db"], pending, o["users"]["employee"])
    assert pending.status == "CANCELLED"
    assert notes(o, "head")[-1].type == "WFH_CANCELLED"


def test_wfh_api(client, org):
    o = org
    day = next_working_day(o["db"], date.today() + timedelta(days=3))
    emp, head = token_for(client, "emp"), token_for(client, "head")

    rules = client.get(f"{API}/me/wfh-rules", headers=bearer(emp)).json()
    assert rules == {"min_notice_days": 1, "has_manager": True, "manager_name": "Meena",
                     "chain": "MANAGER_HR"}
    preview = client.get(f"{API}/me/wfh-preview", headers=bearer(emp),
                         params={"from": day.isoformat(), "to": day.isoformat(), "half": "SECOND_HALF"}).json()
    assert Decimal(preview["days"]) == Decimal("0.5") and preview["errors"] == []

    response = client.post(f"{API}/me/wfh-requests", headers=bearer(emp), json={
        "from_date": day.isoformat(), "to_date": day.isoformat(), "reason": "plumber visit"})
    assert response.status_code == 201, response.text
    request_id = response.json()["id"]
    assert response.json()["step"] == "Awaiting manager"
    assert [r["id"] for r in client.get(f"{API}/me/wfh-requests", headers=bearer(emp)).json()] == [request_id]

    inbox = client.get(f"{API}/me/approvals", headers=bearer(head)).json()
    assert [r["id"] for r in inbox["wfh"]] == [request_id]
    assert client.post(f"{API}/me/approvals/wfh/{request_id}/approve", headers=bearer(emp)).status_code == 403
    response = client.post(f"{API}/me/approvals/wfh/{request_id}/approve", headers=bearer(head),
                           json={"comment": "fine"})
    assert response.status_code == 200 and response.json()["status"] == "PENDING_HR"

    assert client.get(f"{API}/wfh-requests", headers=bearer(emp)).status_code == 403
    queue = client.get(f"{API}/wfh-requests", params={"status": "PENDING"}).json()
    assert [r["id"] for r in queue] == [request_id] and queue[0]["manager_comment"] == "fine"
    approved = client.post(f"{API}/wfh-requests/{request_id}/approve", json={"comment": "ok"})
    assert approved.status_code == 200 and approved.json()["status"] == "APPROVED"
    assert client.post(f"{API}/wfh-requests/{request_id}/reject").status_code == 409

    other = next_working_day(o["db"], day + timedelta(days=1))
    recorded = client.post(f"{API}/wfh-requests", json={
        "employee_id": o["employee"].id, "from_date": other.isoformat(), "to_date": other.isoformat(),
        "half": "FIRST_HALF"})
    assert recorded.status_code == 201, recorded.text
    assert recorded.json()["status"] == "APPROVED" and recorded.json()["step"] == "Recorded by HR"
    cancelled = client.post(f"{API}/wfh-requests/{recorded.json()['id']}/cancel", json={"comment": "x"})
    assert cancelled.json()["status"] == "CANCELLED"
    assert client.get(f"{API}/wfh-requests/{request_id}").json()["days"] in ("1", "1.00")
