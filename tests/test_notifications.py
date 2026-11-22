from datetime import date, time, timedelta

import pytest

from app.models.leave import LeaveApplication
from app.models.notification import Notification
from app.services import notifications as notification_service
from app.services.attendance.sync import sync_attendance
from app.services.leave.workflow import (
    apply_leave,
    apply_permission,
    cancel_by_employee,
    cancel_by_hr,
    hr_approve,
    hr_reject,
    manager_decide,
    record_leave_on_behalf,
)
from tests.attendance_fakes import FakeMatrix
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
MON9, TUE10 = date(2026, 11, 9), date(2026, 11, 10)


@pytest.fixture()
def org(db_session):
    db = db_session
    types = seed_types(db)
    manager = make_employee(db, "M1", "Meena")
    employee = make_employee(db, "E1", "Asha", manager=manager, department=make_department(db, "OPS", manager))
    loner = make_employee(db, "E2", "Ravi")
    users = {
        "manager": make_user(db, "mgr", manager),
        "employee": make_user(db, "emp", employee),
        "loner": make_user(db, "loner", loner),
        "hr": make_user(db, "hr2", role="HR"),
    }
    for person in (employee, loner):
        credit(db, person, types["CL"], 12, 2026)
    return {"db": db, "types": types, "manager": manager, "employee": employee, "loner": loner, "users": users}


def apply(o, who="employee", start=MON9):
    return apply_leave(
        o["db"], employee=o[who], user=o["users"][who], leave_type_id=o["types"]["CL"].id, from_date=start,
        from_half="FULL", to_date=start, to_half="FULL", reason="test", today=TODAY,
    )


def inbox(o, who):
    rows = o["db"].query(Notification).filter(Notification.user_id == o["users"][who].id)
    return rows.order_by(Notification.id).all()


def types_for(o, who):
    return [n.type for n in inbox(o, who)]


def test_apply_with_manager_notifies_manager_only(org):
    o = org
    apply(o)
    [note] = inbox(o, "manager")
    assert note.type == "LEAVE_SUBMITTED" and note.link == "/portal/team-approvals"
    assert "Asha (E1)" in note.title and "Casual Leave" in note.message and not note.is_read
    assert inbox(o, "hr") == [] and inbox(o, "employee") == []


def test_apply_without_manager_notifies_hr(org):
    o = org
    apply(o, who="loner")
    apply_permission(o["db"], employee=o["loner"], user=o["users"]["loner"], day=TUE10, from_time=time(10, 0),
                     to_time=time(11, 0), reason="errand", today=TODAY)
    leave, permission = inbox(o, "hr")
    assert leave.type == "LEAVE_SUBMITTED" and leave.link == "/app/leave/requests"
    assert permission.type == "PERMISSION_SUBMITTED" and permission.link == "/app/leave/permissions"
    assert "60 min" in permission.message
    assert inbox(o, "manager") == []


def test_manager_approve_notifies_hr_and_employee(org):
    o = org
    application = apply(o)
    manager_decide(o["db"], application, o["users"]["manager"], True, "fine by me")
    [hr_note] = inbox(o, "hr")
    assert hr_note.type == "LEAVE_PENDING_HR" and "awaiting HR approval" in hr_note.title
    assert hr_note.link == "/app/leave/requests" and "fine by me" in hr_note.message
    [emp_note] = inbox(o, "employee")
    assert emp_note.type == "LEAVE_MANAGER_APPROVED" and emp_note.link == "/portal/requests"
    assert types_for(o, "manager") == ["LEAVE_SUBMITTED"]


def test_hr_approve_and_reject_notify_employee(org):
    o = org
    first = apply(o, start=MON9)
    second = apply(o, start=TUE10)
    # The manager step is done; HR decides at PENDING_HR.
    first.status = second.status = "PENDING_HR"
    o["db"].flush()
    hr_approve(o["db"], first, o["users"]["hr"], "ok")
    hr_reject(o["db"], second, o["users"]["hr"], "short staffed")
    approved, rejected = inbox(o, "employee")
    assert approved.type == "LEAVE_APPROVED" and approved.title == "Your leave request was approved"
    assert rejected.type == "LEAVE_REJECTED" and "short staffed" in rejected.message
    assert inbox(o, "hr") == []


def test_manager_reject_notifies_employee_only(org):
    o = org
    application = apply(o)
    manager_decide(o["db"], application, o["users"]["manager"], False, "busy week")
    [note] = inbox(o, "employee")
    assert note.type == "LEAVE_REJECTED" and "manager" in note.title and "busy week" in note.message
    assert inbox(o, "hr") == []


def test_cancel_notifies_whoever_it_was_pending_with(org):
    o = org
    pending_manager = apply(o, start=MON9)
    cancel_by_employee(o["db"], pending_manager, o["users"]["employee"], "plans changed")
    assert types_for(o, "manager") == ["LEAVE_SUBMITTED", "LEAVE_CANCELLED"]
    assert "plans changed" in inbox(o, "manager")[-1].message

    pending_hr = apply(o, start=TUE10)
    manager_decide(o["db"], pending_hr, o["users"]["manager"], True, None)
    cancel_by_employee(o["db"], pending_hr, o["users"]["employee"])
    assert types_for(o, "hr") == ["LEAVE_PENDING_HR", "LEAVE_CANCELLED"]
    assert types_for(o, "manager") == ["LEAVE_SUBMITTED", "LEAVE_CANCELLED", "LEAVE_SUBMITTED"]


def test_hr_record_and_cancel_notify_employee(org):
    o = org
    application, _ = record_leave_on_behalf(
        o["db"], hr_user=o["users"]["hr"], employee_id=o["employee"].id, leave_type_id=o["types"]["CL"].id,
        from_date=MON9, from_half="FULL", to_date=MON9, to_half="FULL", reason=None, today=TODAY,
    )
    cancel_by_hr(o["db"], application, o["users"]["hr"], "entered by mistake")
    assert types_for(o, "employee") == ["LEAVE_RECORDED", "LEAVE_CANCELLED"]
    assert inbox(o, "manager") == []


def test_notification_failure_never_fails_the_action(org, monkeypatch):
    o = org

    def boom(**kwargs):
        raise RuntimeError("notifications down")

    monkeypatch.setattr(notification_service, "Notification", boom)
    application = apply(o)
    o["db"].commit()
    assert application.status == "PENDING_MANAGER"
    assert o["db"].query(LeaveApplication).count() == 1
    assert o["db"].query(Notification).count() == 0


def test_sync_failure_notifies_hr(org):
    o = org
    fake = FakeMatrix()
    fake.fail_with = 500
    run = sync_attendance(o["db"], MON9, MON9, client=fake.client())
    assert run.status == "failed"
    [note] = inbox(o, "hr")
    assert note.type == "ATTENDANCE_SYNC_FAILED" and note.link == "/app/attendance/attendance-log"
    assert "09 Nov 2026" in note.message
    assert inbox(o, "employee") == []


# --- API ---------------------------------------------------------------------

def _seed(db, user_id, count, **extra):
    rows = [Notification(user_id=user_id, type="TEST", title=f"Note {i}", link="/app/dashboard", **extra)
            for i in range(count)]
    db.add_all(rows)
    db.commit()
    return rows


def test_list_returns_own_notifications_only(client, db_session, seeded_admin):
    other = make_user(db_session, "someone", role="HR")
    mine = _seed(db_session, seeded_admin.id, 3)
    _seed(db_session, other.id, 2)
    response = client.get(f"{API}/notifications")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unread_count"] == 3
    assert [item["id"] for item in body["items"]] == [n.id for n in reversed(mine)]
    paged = client.get(f"{API}/notifications", params={"limit": 2, "before_id": mine[2].id}).json()
    assert [item["id"] for item in paged["items"]] == [mine[1].id, mine[0].id]


def test_unread_count_mark_read_and_read_all(client, db_session, seeded_admin):
    other = make_user(db_session, "someone", role="HR")
    first, second, third = _seed(db_session, seeded_admin.id, 3)
    [theirs] = _seed(db_session, other.id, 1)
    assert client.get(f"{API}/notifications/unread-count").json() == {"unread_count": 3}

    response = client.post(f"{API}/notifications/{first.id}/read")
    assert response.status_code == 200 and response.json()["is_read"] is True
    assert response.json()["read_at"] is not None
    assert client.get(f"{API}/notifications/unread-count").json() == {"unread_count": 2}
    unread = client.get(f"{API}/notifications", params={"unread_only": True}).json()["items"]
    assert {item["id"] for item in unread} == {second.id, third.id}

    assert client.post(f"{API}/notifications/{theirs.id}/read").status_code == 404
    assert client.post(f"{API}/notifications/999999/read").status_code == 404

    result = client.post(f"{API}/notifications/read-all").json()
    assert result == {"updated": 2, "unread_count": 0}
    db_session.refresh(theirs)
    assert theirs.is_read is False


def test_employee_login_can_use_notifications(client, org):
    o = org
    apply(o)
    o["db"].commit()
    headers = bearer(token_for(client, "mgr"))
    body = client.get(f"{API}/notifications", headers=headers).json()
    assert body["unread_count"] == 1 and body["items"][0]["link"] == "/portal/team-approvals"
    note_id = body["items"][0]["id"]
    assert client.post(f"{API}/notifications/{note_id}/read", headers=headers).status_code == 200
    assert client.get(f"{API}/notifications/unread-count", headers=headers).json() == {"unread_count": 0}
    assert client.get(f"{API}/notifications", headers={"Authorization": ""}).status_code == 401


def test_leave_api_creates_notifications(client, org):
    o = org
    headers = bearer(token_for(client, "loner"))
    day = next_working_day(o["db"], date.today() + timedelta(days=7)).isoformat()
    response = client.post(f"{API}/me/leave-applications", headers=headers, json={
        "leave_type_id": o["types"]["LOP"].id, "from_date": day, "to_date": day,
        "from_half": "FULL", "to_half": "FULL", "reason": "trip",
    })
    assert response.status_code == 201, response.text
    # Both HR logins (the seeded admin and hr2) are told.
    admin_inbox = client.get(f"{API}/notifications").json()
    assert admin_inbox["unread_count"] == 1
    assert admin_inbox["items"][0]["type"] == "LEAVE_SUBMITTED"
    assert len(inbox(o, "hr")) == 1


def test_me_reports_last_login(client):
    body = client.get(f"{API}/auth/me").json()
    assert body["last_login_at"] is not None
