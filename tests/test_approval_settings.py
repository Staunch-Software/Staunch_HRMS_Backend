"""Approval Workflow settings: chain per request type, the no-manager
fallback, re-routing when a manager becomes eligible, and payroll-run
maker-checker."""
from datetime import date, time
from decimal import Decimal

import pytest

from app.models.leave import LeaveLedger
from app.models.notification import Notification
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun
from app.services import approval_settings
from app.services.leave.wfh import apply_wfh
from app.services.leave.workflow import (
    WorkflowError,
    apply_leave,
    apply_permission,
    approving_manager,
    hr_approve,
    manager_decide,
    record_permission_on_behalf,
    reroute_pending,
)
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
    boss = make_employee(db, "R1", "Rahul")
    ops = make_department(db, "OPS", head)
    employee = make_employee(db, "E1", "Asha", manager=boss, department=ops)
    users = {"head": make_user(db, "head", head), "boss": make_user(db, "boss", boss),
             "employee": make_user(db, "emp", employee), "hr": make_user(db, "hr2", role="HR")}
    credit(db, employee, types["CL"], 12, 2026)
    return {"db": db, "types": types, "head": head, "boss": boss, "ops": ops, "employee": employee, "users": users}


def configure(db, **values):
    approval_settings.update_settings(db, values)
    db.commit()


def leave(o, start=MON9):
    return apply_leave(o["db"], employee=o["employee"], user=o["users"]["employee"],
                       leave_type_id=o["types"]["CL"].id, from_date=start, from_half="FULL", to_date=start,
                       to_half="FULL", reason=None, today=TODAY)


def permission(o, day=TUE10):
    return apply_permission(o["db"], employee=o["employee"], user=o["users"]["employee"], day=day,
                            from_time=time(9, 30), to_time=time(10, 0), reason=None, today=TODAY)


def wfh(o, day=TUE10):
    return apply_wfh(o["db"], employee=o["employee"], user=o["users"]["employee"], from_date=day, to_date=day,
                     half="FULL", reason=None, today=TODAY)


def test_settings_api_defaults_and_update(client, org):
    defaults = client.get(f"{API}/settings/approvals").json()
    assert defaults["leave_chain"] == "MANAGER_HR" and defaults["no_manager_fallback"] == "HR"
    assert defaults["payroll_maker_checker"] is False
    body = {**defaults, "permission_chain": "HR_ONLY", "wfh_chain": "MANAGER_ONLY",
            "no_manager_fallback": "REPORTING_MANAGER", "payroll_maker_checker": True}
    saved = client.put(f"{API}/settings/approvals", json=body)
    assert saved.status_code == 200, saved.text
    again = client.get(f"{API}/settings/approvals").json()
    assert again["permission_chain"] == "HR_ONLY" and again["wfh_chain"] == "MANAGER_ONLY"
    assert again["payroll_maker_checker"] is True
    assert client.put(f"{API}/settings/approvals", json={**body, "leave_chain": "BOSS"}).status_code == 422
    employee = token_for(client, "emp")
    assert client.get(f"{API}/settings/approvals", headers=bearer(employee)).status_code == 403


@pytest.mark.parametrize("kind,apply", [("leave_chain", leave), ("permission_chain", permission),
                                         ("wfh_chain", wfh)])
def test_hr_only_chain_skips_manager(org, kind, apply):
    configure(org["db"], **{kind: "HR_ONLY"})
    request = apply(org)
    assert request.status == "PENDING_HR" and request.manager_id is None


@pytest.mark.parametrize("kind,apply", [("leave_chain", leave), ("permission_chain", permission),
                                         ("wfh_chain", wfh)])
def test_manager_hr_chain_needs_hr_after_manager(org, kind, apply):
    configure(org["db"], **{kind: "MANAGER_HR"})
    request = apply(org)
    assert request.status == "PENDING_MANAGER" and request.manager_id == org["head"].id
    manager_decide(org["db"], request, org["users"]["head"], True, None)
    assert request.status == "PENDING_HR"


@pytest.mark.parametrize("kind,apply", [("leave_chain", leave), ("permission_chain", permission),
                                         ("wfh_chain", wfh)])
def test_manager_only_chain_approves_outright(org, kind, apply):
    o = org
    configure(o["db"], **{kind: "MANAGER_ONLY"})
    request = apply(o)
    assert request.status == "PENDING_MANAGER"
    manager_decide(o["db"], request, o["users"]["head"], True, "ok")
    assert request.status == "APPROVED" and request.hr_action_by is None
    note = o["db"].query(Notification).filter(Notification.user_id == o["users"]["employee"].id).all()
    assert any("approved" in n.title for n in note)


def test_manager_only_leave_debits_balance_and_hr_cannot_skip_the_manager(org):
    o = org
    configure(o["db"], leave_chain="MANAGER_ONLY")
    first = leave(o)
    manager_decide(o["db"], first, o["users"]["head"], True, None)
    debits = o["db"].query(LeaveLedger).filter(LeaveLedger.entry_type == "LEAVE_DEBIT").all()
    assert [(d.reference_id, d.days) for d in debits] == [(str(first.id), Decimal("-1"))]
    second = leave(o, start=TUE10)
    with pytest.raises(WorkflowError):
        hr_approve(o["db"], second, o["users"]["hr"], None)
    assert second.status == "PENDING_MANAGER"


def test_reporting_manager_fallback(org, client):
    o = org
    o["users"]["head"].is_active = False
    o["db"].commit()
    assert leave(o).status == "PENDING_HR"
    configure(o["db"], no_manager_fallback="REPORTING_MANAGER")
    application = leave(o, start=TUE10)
    assert application.status == "PENDING_MANAGER" and application.manager_id == o["boss"].id
    boss = token_for(client, "boss")
    inbox = client.get(f"{API}/me/approvals", headers=bearer(boss)).json()
    assert [a["id"] for a in inbox["leave"]] == [application.id]
    response = client.post(f"{API}/me/approvals/leave/{application.id}/approve", headers=bearer(boss))
    assert response.status_code == 200 and response.json()["status"] == "PENDING_HR"


def test_fallback_not_used_when_department_manager_eligible(org):
    configure(org["db"], no_manager_fallback="REPORTING_MANAGER")
    assert approving_manager(org["db"], org["employee"], "LEAVE").id == org["head"].id


def test_department_warns_when_manager_has_no_login(client, org):
    o = org
    lone = make_employee(o["db"], "M2", "Murugadasan")
    response = client.put(f"{API}/departments/{o['ops'].id}",
                          json={"name": "Ops", "code": "OPS", "manager_employee_id": lone.id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["manager_has_login"] is False and "has no login" in body["warning"]
    listed = {d["id"]: d for d in client.get(f"{API}/departments").json()}
    assert listed[o["ops"].id]["manager_has_login"] is False
    head = client.put(f"{API}/departments/{o['ops'].id}",
                      json={"name": "Ops", "code": "OPS", "manager_employee_id": o["head"].id}).json()
    assert head["manager_has_login"] is True and head["warning"] is None


def test_reroute_after_login_created(client, org):
    o = org
    lone = make_employee(o["db"], "M2", "Murugadasan")
    o["ops"].manager_employee_id = lone.id
    o["db"].commit()
    stuck_permission, stuck_leave, stuck_wfh = permission(o), leave(o), wfh(o, day=date(2026, 11, 11))
    assert {r.status for r in (stuck_permission, stuck_leave, stuck_wfh)} == {"PENDING_HR"}
    recorded, _ = record_permission_on_behalf(o["db"], hr_user=o["users"]["hr"], employee_id=o["employee"].id,
                                              day=date(2026, 11, 12), from_time=time(9, 30), to_time=time(10, 0),
                                              reason=None, today=TODAY)
    o["db"].commit()

    created = client.post(f"{API}/employee-logins/bulk-create", json={"employee_ids": [lone.id]})
    assert created.status_code == 200 and len(created.json()["created"]) == 1
    for request in (stuck_permission, stuck_leave, stuck_wfh, recorded):
        o["db"].refresh(request)
    assert {r.status for r in (stuck_permission, stuck_leave, stuck_wfh)} == {"PENDING_MANAGER"}
    assert stuck_permission.manager_id == lone.id
    assert recorded.status == "APPROVED"
    manager_user = created.json()["created"][0]["username"]
    from app.models.user import AdminUser
    user_id = o["db"].query(AdminUser.id).filter(AdminUser.username == manager_user).scalar()
    assert o["db"].query(Notification).filter(Notification.user_id == user_id).count() == 3


def test_reroute_after_login_enabled_and_department_change(client, org):
    o = org
    o["users"]["head"].is_active = False
    o["db"].commit()
    stuck = permission(o)
    assert stuck.status == "PENDING_HR"
    assert client.post(f"{API}/employee-logins/{o['head'].id}/enable").status_code == 200
    o["db"].refresh(stuck)
    assert stuck.status == "PENDING_MANAGER" and stuck.manager_id == o["head"].id

    o["ops"].manager_employee_id = None
    o["db"].commit()
    other = leave(o)
    assert other.status == "PENDING_HR"
    response = client.put(f"{API}/departments/{o['ops'].id}",
                          json={"name": "Ops", "code": "OPS", "manager_employee_id": o["head"].id})
    assert response.status_code == 200
    o["db"].refresh(other)
    assert other.status == "PENDING_MANAGER"


def test_reroute_respects_hr_only_chain(org):
    o = org
    configure(o["db"], permission_chain="HR_ONLY")
    stuck = permission(o)
    assert reroute_pending(o["db"], o["ops"].id) == 0
    assert stuck.status == "PENDING_HR"


def test_permission_rules_report_manager(client, org):
    o = org
    rules = client.get(f"{API}/me/leave-rules", headers=bearer(token_for(client, "emp"))).json()
    assert rules["permission_has_manager"] is True and rules["permission_manager_name"] == "Meena"
    configure(o["db"], permission_chain="HR_ONLY")
    rules = client.get(f"{API}/me/leave-rules", headers=bearer(token_for(client, "emp"))).json()
    assert rules["permission_has_manager"] is False and rules["has_manager"] is True


def test_pending_counts(client, org):
    leave(org)
    permission(org)
    org["db"].commit()
    assert client.get(f"{API}/leave/pending-counts").json() == {"leave": 1, "permissions": 1, "wfh": 0,
                                                                   "regularizations": 0}


# --- payroll maker-checker ---------------------------------------------------

@pytest.fixture()
def run(db_session):
    schedule = PaySchedule(schedule_name="Monthly", pay_frequency="Monthly", payment_day_rule={"type": "LastDayOfMonth"},
                           holiday_adjustment="Before", is_active=True, applies_to=None)
    db_session.add(schedule)
    db_session.commit()
    row = PayrollRun(pay_schedule_id=schedule.id, period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
                     pay_date=date(2026, 10, 31), status="Calculated", calculated_by="admin")
    db_session.add(row)
    db_session.commit()
    return row


def test_maker_checker_off_keeps_direct_approval(client, run):
    response = client.post(f"{API}/payroll-runs/{run.id}/approve", json={})
    assert response.status_code == 200 and response.json()["approved_by"] == "admin"


def test_maker_checker_same_user_cannot_approve(client, db_session, run):
    configure(db_session, payroll_maker_checker=True)
    assert client.post(f"{API}/payroll-runs/{run.id}/approve", json={}).status_code == 400
    submitted = client.post(f"{API}/payroll-runs/{run.id}/submit")
    assert submitted.status_code == 200 and submitted.json()["status"] == "PendingApproval"
    assert submitted.json()["submitted_by"] == "admin"
    assert client.post(f"{API}/payroll-runs/{run.id}/submit").status_code == 400
    response = client.post(f"{API}/payroll-runs/{run.id}/approve", json={"approved_by": "someone"})
    assert response.status_code == 403

    make_user(db_session, "checker", role="HR")
    checker = token_for(client, "checker")
    response = client.post(f"{API}/payroll-runs/{run.id}/approve", json={}, headers=bearer(checker))
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "Approved" and response.json()["approved_by"] == "checker"


def test_maker_checker_blocks_submitter_who_did_not_calculate(client, db_session, run):
    configure(db_session, payroll_maker_checker=True)
    make_user(db_session, "checker", role="HR")
    checker = token_for(client, "checker")
    assert client.post(f"{API}/payroll-runs/{run.id}/submit", headers=bearer(checker)).status_code == 200
    assert client.post(f"{API}/payroll-runs/{run.id}/approve", json={},
                       headers=bearer(checker)).status_code == 403
    assert client.post(f"{API}/payroll-runs/{run.id}/approve", json={}).status_code == 403
    make_user(db_session, "third", role="HR")
    third = token_for(client, "third")
    assert client.post(f"{API}/payroll-runs/{run.id}/approve", json={}, headers=bearer(third)).status_code == 200
