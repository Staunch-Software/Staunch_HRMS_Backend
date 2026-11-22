"""Leave, permission and WFH approvals go to the employee's department
manager; every fallback goes straight to HR."""
from datetime import date, time

import pytest

from app.models.notification import Notification
from app.services.leave.wfh import apply_wfh
from app.services.leave.workflow import WorkflowError, apply_leave, apply_permission, manager_decide
from tests.leave_helpers import (
    API,
    bearer,
    credit,
    make_department,
    make_employee,
    make_user,
    seed_types,
    token_for,
)

TODAY = date(2026, 11, 2)  # Monday
MON9, TUE10 = date(2026, 11, 9), date(2026, 11, 10)


@pytest.fixture()
def org(db_session):
    db = db_session
    types = seed_types(db)
    head = make_employee(db, "M1", "Meena")
    boss = make_employee(db, "R1", "Rahul")  # reporting manager only
    ops = make_department(db, "OPS", head)
    head.department_id = ops.id
    employee = make_employee(db, "E1", "Asha", manager=boss, department=ops)
    users = {"head": make_user(db, "head", head), "boss": make_user(db, "boss", boss),
             "employee": make_user(db, "emp", employee), "hr": make_user(db, "hr2", role="HR")}
    for person in (employee, head):
        credit(db, person, types["CL"], 12, 2026)
    return {"db": db, "types": types, "head": head, "boss": boss, "ops": ops, "employee": employee, "users": users}


def leave(o, who="employee", start=MON9):
    return apply_leave(o["db"], employee=o[who], user=o["users"][who], leave_type_id=o["types"]["CL"].id,
                       from_date=start, from_half="FULL", to_date=start, to_half="FULL", reason=None, today=TODAY)


def test_department_manager_not_reporting_manager_approves(org):
    o = org
    application = leave(o)
    assert application.status == "PENDING_MANAGER" and application.manager_id == o["head"].id
    with pytest.raises(WorkflowError) as exc:
        manager_decide(o["db"], application, o["users"]["boss"], True, None)
    assert exc.value.status_code == 403
    manager_decide(o["db"], application, o["users"]["head"], True, None)
    assert application.status == "PENDING_HR"
    notes = o["db"].query(Notification).filter(Notification.type == "LEAVE_SUBMITTED").all()
    assert [n.user_id for n in notes] == [o["users"]["head"].id]

    permission = apply_permission(o["db"], employee=o["employee"], user=o["users"]["employee"], day=TUE10,
                                  from_time=time(9, 30), to_time=time(10, 0), reason=None, today=TODAY)
    assert permission.status == "PENDING_MANAGER" and permission.manager_id == o["head"].id
    wfh = apply_wfh(o["db"], employee=o["employee"], user=o["users"]["employee"], from_date=TUE10, to_date=TUE10,
                    half="FULL", reason=None, today=TODAY)
    assert wfh.status == "PENDING_MANAGER" and wfh.manager_id == o["head"].id


def test_no_department_goes_to_hr(org):
    o = org
    o["employee"].department_id = None
    o["db"].commit()
    assert leave(o).status == "PENDING_HR"


def test_department_without_manager_goes_to_hr(org):
    o = org
    o["ops"].manager_employee_id = None
    o["db"].commit()
    assert leave(o).status == "PENDING_HR"


def test_manager_own_request_goes_to_hr(org):
    o = org
    application = leave(o, who="head")
    assert application.status == "PENDING_HR" and application.manager_id is None


def test_inactive_manager_or_login_goes_to_hr(org):
    o = org
    o["head"].status = "Inactive"
    o["db"].commit()
    assert leave(o, start=MON9).status == "PENDING_HR"
    o["head"].status = "Active"
    o["users"]["head"].is_active = False
    o["db"].commit()
    assert leave(o, start=TUE10).status == "PENDING_HR"


def test_manager_without_login_goes_to_hr(org):
    o = org
    lone = make_employee(o["db"], "M2", "Nila")
    o["ops"].manager_employee_id = lone.id
    o["db"].commit()
    assert leave(o).status == "PENDING_HR"


def test_pending_request_follows_current_department_manager(org, client):
    o = org
    application = leave(o)
    new_head = make_employee(o["db"], "M3", "Priya")
    make_user(o["db"], "newhead", new_head)
    o["ops"].manager_employee_id = new_head.id
    o["db"].commit()
    old, new = token_for(client, "head"), token_for(client, "newhead")
    assert client.get(f"{API}/me/approvals", headers=bearer(old)).json()["leave"] == []
    assert [a["id"] for a in client.get(f"{API}/me/approvals", headers=bearer(new)).json()["leave"]] == [
        application.id]
    assert client.post(f"{API}/me/approvals/leave/{application.id}/approve", headers=bearer(old)).status_code == 403
    response = client.post(f"{API}/me/approvals/leave/{application.id}/approve", headers=bearer(new))
    assert response.status_code == 200 and response.json()["status"] == "PENDING_HR"


def test_has_reports_for_department_manager(org, client):
    o = org
    assert client.get(f"{API}/auth/me", headers=bearer(token_for(client, "head"))).json()["has_reports"] is True
    assert client.get(f"{API}/me/profile", headers=bearer(token_for(client, "head"))).json()["has_reports"] is True
    # The reporting manager still sees Team Approvals (direct reports), but no requests.
    boss = token_for(client, "boss")
    assert client.get(f"{API}/auth/me", headers=bearer(boss)).json()["has_reports"] is True
    leave(o)
    assert client.get(f"{API}/me/approvals", headers=bearer(boss)).json() == {"leave": [], "permissions": [],
                                                                            "wfh": [], "regularizations": []}
    assert client.get(f"{API}/auth/me", headers=bearer(token_for(client, "emp"))).json()["has_reports"] is False
