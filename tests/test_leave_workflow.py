from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.models.leave import LeaveLedger
from app.services.leave import approved_leave_by_day
from app.services.leave.balance import balance, leave_year_for
from app.services.leave.workflow import (
    WorkflowError,
    apply_leave,
    cancel_by_employee,
    cancel_by_hr,
    hr_approve,
    hr_reject,
    manager_decide,
    record_leave_on_behalf,
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
MON9, TUE10, WED11 = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)


@pytest.fixture()
def org(db_session):
    db = db_session
    types = seed_types(db)
    manager = make_employee(db, "M1", "Meena")
    ops = make_department(db, "OPS", manager)
    employee = make_employee(db, "E1", "Asha", manager=manager, department=ops)
    loner = make_employee(db, "E2", "Ravi")
    other = make_employee(db, "E3", "Kiran", manager=manager, department=ops)
    users = {
        "manager": make_user(db, "mgr", manager),
        "employee": make_user(db, "emp", employee),
        "loner": make_user(db, "loner", loner),
        "other": make_user(db, "other", other),
        "hr": make_user(db, "hr2", role="HR"),
    }
    for person in (employee, loner, other):
        credit(db, person, types["CL"], 12, 2026)
    return {"db": db, "types": types, "manager": manager, "employee": employee, "loner": loner, "other": other,
            "users": users}


def apply(o, who="employee", type_code="CL", start=MON9, end=None, from_half="FULL", to_half="FULL", today=TODAY):
    return apply_leave(
        o["db"], employee=o[who], user=o["users"][who], leave_type_id=o["types"][type_code].id, from_date=start,
        from_half=from_half, to_date=end or start, to_half=to_half, reason="test", today=today,
    )


def cl_balance(o, who="employee"):
    return balance(o["db"], o[who].id, o["types"]["CL"].id, 2026)


def test_manager_then_hr_flow_debits_balance(org):
    o = org
    application = apply(o, start=MON9, end=TUE10)
    assert application.status == "PENDING_MANAGER" and application.days == Decimal("2")
    assert application.manager_id == o["manager"].id

    manager_decide(o["db"], application, o["users"]["manager"], True, "ok")
    assert application.status == "PENDING_HR" and application.manager_comment == "ok"
    assert cl_balance(o) == Decimal("12")

    hr_approve(o["db"], application, o["users"]["hr"], "fine")
    assert application.status == "APPROVED"
    assert cl_balance(o) == Decimal("10")
    by_day = approved_leave_by_day(o["db"], o["employee"].id, MON9, WED11)
    assert set(by_day) == {MON9, TUE10}
    assert by_day[MON9][0].leave_type.code == "CL" and by_day[MON9][0].fraction == Decimal("1")


def test_no_manager_goes_straight_to_hr(org):
    o = org
    assert apply(o, who="loner").status == "PENDING_HR"
    # A manager without an active login cannot act, so HR gets it directly.
    o["users"]["manager"].is_active = False
    o["db"].commit()
    assert apply(o, start=TUE10).status == "PENDING_HR"


def test_hr_decides_only_after_the_manager(org):
    o = org
    first = apply(o, start=MON9)
    with pytest.raises(WorkflowError):
        hr_approve(o["db"], first, o["users"]["hr"], None)
    with pytest.raises(WorkflowError):
        hr_reject(o["db"], first, o["users"]["hr"], "no")
    assert first.status == "PENDING_MANAGER" and cl_balance(o) == Decimal("12")
    manager_decide(o["db"], first, o["users"]["manager"], True, None)
    hr_approve(o["db"], first, o["users"]["hr"], None)
    assert first.status == "APPROVED" and cl_balance(o) == Decimal("11")

    second = apply(o, start=TUE10)
    manager_decide(o["db"], second, o["users"]["manager"], False, "busy week")
    assert second.status == "REJECTED"
    with pytest.raises(WorkflowError):
        hr_approve(o["db"], second, o["users"]["hr"], None)

    third = apply(o, start=WED11)
    manager_decide(o["db"], third, o["users"]["manager"], True, None)
    hr_reject(o["db"], third, o["users"]["hr"], "no")
    assert third.status == "REJECTED" and cl_balance(o) == Decimal("11")


def test_cancel_pending_and_hr_cancel_reverses_ledger(org):
    o = org
    pending = apply(o, start=MON9)
    cancel_by_employee(o["db"], pending, o["users"]["employee"])
    assert pending.status == "CANCELLED"

    approved = apply(o, start=TUE10, end=WED11)
    manager_decide(o["db"], approved, o["users"]["manager"], True, None)
    hr_approve(o["db"], approved, o["users"]["hr"], None)
    assert cl_balance(o) == Decimal("10")
    with pytest.raises(WorkflowError) as exc:
        cancel_by_employee(o["db"], approved, o["users"]["employee"])
    assert exc.value.status_code == 409
    with pytest.raises(WorkflowError) as exc:
        cancel_by_employee(o["db"], approved, o["users"]["other"])
    assert exc.value.status_code == 404

    cancel_by_hr(o["db"], approved, o["users"]["hr"], "plans changed")
    assert approved.status == "CANCELLED" and cl_balance(o) == Decimal("12")
    entries = o["db"].query(LeaveLedger).filter(LeaveLedger.reference_id == str(approved.id)).all()
    assert sorted(e.entry_type for e in entries) == ["LEAVE_DEBIT", "REVERSAL"]
    # Re-cancelling does nothing more.
    with pytest.raises(WorkflowError):
        cancel_by_hr(o["db"], approved, o["users"]["hr"])


def test_insufficient_balance_blocked(org):
    o = org
    with pytest.raises(WorkflowError, match="Insufficient Sick Leave balance"):
        apply(o, type_code="SL", start=MON9)  # no SL credited
    credit(o["db"], o["employee"], o["types"]["SL"], 1, 2026)
    apply(o, type_code="SL", start=MON9)
    # The pending day is reserved, so a second day is refused.
    with pytest.raises(WorkflowError, match="0 day\\(s\\) available"):
        apply(o, type_code="SL", start=TUE10)
    # Loss of Pay never checks a balance.
    assert apply(o, type_code="LOP", start=TUE10).days == Decimal("1")


def test_balance_rechecked_at_final_approval(org):
    o = org
    credit(o["db"], o["employee"], o["types"]["SL"], 1, 2026)
    application = apply(o, type_code="SL", start=MON9)
    manager_decide(o["db"], application, o["users"]["manager"], True, None)
    credit(o["db"], o["employee"], o["types"]["SL"], -1, 2026)
    with pytest.raises(WorkflowError, match="Insufficient"):
        hr_approve(o["db"], application, o["users"]["hr"], None)
    assert application.status == "PENDING_HR"


def test_notice_period_blocked_for_employee_but_hr_can_record(org):
    o = org
    o["employee"].notice_start_date = date(2026, 11, 10)
    o["db"].commit()
    with pytest.raises(WorkflowError, match="notice period"):
        apply(o, start=TUE10)
    with pytest.raises(WorkflowError, match="notice period"):
        apply(o, start=MON9, end=TUE10)
    assert apply(o, start=MON9).status == "PENDING_MANAGER"

    application, warnings = record_leave_on_behalf(
        o["db"], hr_user=o["users"]["hr"], employee_id=o["employee"].id, leave_type_id=o["types"]["CL"].id,
        from_date=TUE10, from_half="FULL", to_date=WED11, to_half="FULL", reason=None, today=TODAY,
    )
    assert application.status == "APPROVED" and application.days == Decimal("2")
    assert any("notice period" in w for w in warnings)
    assert cl_balance(o) == Decimal("12")  # notice-period days are not debited

    o["types"]["SL"].allowed_during_notice = True
    credit(o["db"], o["employee"], o["types"]["SL"], 3, 2026)
    assert apply(o, type_code="SL", start=date(2026, 11, 12)).status == "PENDING_MANAGER"


def test_min_notice_and_backdated_blocked_for_employee_allowed_for_hr(org):
    o = org
    with pytest.raises(WorkflowError, match="at least 1 day"):
        apply(o, start=TODAY)
    with pytest.raises(WorkflowError, match="Backdated"):
        apply(o, start=date(2026, 10, 30))
    assert apply(o, start=TODAY + timedelta(days=1)).status == "PENDING_MANAGER"

    application, warnings = record_leave_on_behalf(
        o["db"], hr_user=o["users"]["hr"], employee_id=o["employee"].id, leave_type_id=o["types"]["CL"].id,
        from_date=date(2026, 10, 30), from_half="FULL", to_date=date(2026, 10, 30), to_half="FULL",
        reason="was sick", comment="phoned in", today=TODAY,
    )
    assert application.status == "APPROVED" and application.recorded_by_hr
    assert any("Backdated" in w for w in warnings)
    assert cl_balance(o) == Decimal("11")

    _, warnings = record_leave_on_behalf(
        o["db"], hr_user=o["users"]["hr"], employee_id=o["employee"].id, leave_type_id=o["types"]["CL"].id,
        from_date=TODAY, from_half="FIRST_HALF", to_date=TODAY, to_half="FIRST_HALF", reason=None, today=TODAY,
    )
    assert any("minimum notice" in w for w in warnings)


def test_overlap_is_half_day_aware(org):
    o = org
    apply(o, start=MON9)
    with pytest.raises(WorkflowError, match="overlaps"):
        apply(o, start=MON9, from_half="SECOND_HALF")
    with pytest.raises(WorkflowError, match="overlaps"):
        apply(o, start=date(2026, 11, 6), end=MON9)
    apply(o, start=TUE10, from_half="FIRST_HALF")
    second_half = apply(o, start=TUE10, from_half="SECOND_HALF")
    assert second_half.days == Decimal("0.5")
    # A rejected request frees its dates.
    manager_decide(o["db"], second_half, o["users"]["manager"], False, None)
    assert apply(o, start=TUE10, from_half="SECOND_HALF").days == Decimal("0.5")


def test_non_manager_and_self_approval_refused(org):
    o = org
    application = apply(o, start=MON9)
    with pytest.raises(WorkflowError) as exc:
        manager_decide(o["db"], application, o["users"]["other"], True, None)
    assert exc.value.status_code == 403
    hr_employee = make_employee(o["db"], "H1", "Hari")
    hr_user = make_user(o["db"], "hr-emp", hr_employee, role="HR")
    credit(o["db"], hr_employee, o["types"]["CL"], 5, 2026)
    own = apply_leave(o["db"], employee=hr_employee, user=hr_user, leave_type_id=o["types"]["CL"].id,
                      from_date=MON9, from_half="FULL", to_date=MON9, to_half="FULL", reason=None, today=TODAY)
    with pytest.raises(WorkflowError) as exc:
        hr_approve(o["db"], own, hr_user, None)
    assert exc.value.status_code == 403


# --- API --------------------------------------------------------------------

def test_api_flow_manager_then_hr(client, org):
    o = org
    day = next_working_day(o["db"], date.today() + timedelta(days=2))
    year = leave_year_for(o["db"], day)
    credit(o["db"], o["employee"], o["types"]["CL"], 5, year)
    emp, mgr, other = (token_for(client, name) for name in ("emp", "mgr", "other"))

    preview = client.get(f"{API}/me/leave-days-preview", headers=bearer(emp),
                         params={"type": o["types"]["CL"].id, "from": day.isoformat(), "to": day.isoformat()})
    assert preview.status_code == 200, preview.text
    assert Decimal(preview.json()["days"]) == Decimal("1")

    response = client.post(f"{API}/me/leave-applications", headers=bearer(emp), json={
        "leave_type_id": o["types"]["CL"].id, "from_date": day.isoformat(), "to_date": day.isoformat(),
        "reason": "family function",
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "PENDING_MANAGER" and body["step"] == "Awaiting manager"
    application_id = body["id"]

    backdated = client.post(f"{API}/me/leave-applications", headers=bearer(emp), json={
        "leave_type_id": o["types"]["CL"].id, "from_date": (date.today() - timedelta(days=3)).isoformat(),
        "to_date": (date.today() - timedelta(days=3)).isoformat(),
    })
    assert backdated.status_code == 400 and "Backdated" in backdated.json()["detail"]

    inbox = client.get(f"{API}/me/approvals", headers=bearer(mgr)).json()
    assert [a["id"] for a in inbox["leave"]] == [application_id]
    assert client.get(f"{API}/me/approvals", headers=bearer(other)).json() == {
        "leave": [], "permissions": [], "wfh": [], "regularizations": []}

    assert client.post(f"{API}/me/approvals/leave/{application_id}/approve", headers=bearer(other),
                       json={"comment": "x"}).status_code == 403
    assert client.post(f"{API}/leave-applications/{application_id}/approve", headers=bearer(mgr)).status_code == 403

    response = client.post(f"{API}/me/approvals/leave/{application_id}/approve", headers=bearer(mgr),
                           json={"comment": "go ahead"})
    assert response.status_code == 200 and response.json()["status"] == "PENDING_HR"

    queue = client.get(f"{API}/leave-applications", params={"status": "PENDING"}).json()
    assert [a["id"] for a in queue] == [application_id] and queue[0]["manager_comment"] == "go ahead"

    response = client.post(f"{API}/leave-applications/{application_id}/approve", json={"comment": "ok"})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "APPROVED"

    balances = client.get(f"{API}/me/balances", headers=bearer(emp), params={"year": year}).json()
    cl = next(b for b in balances["balances"] if b["leave_type_code"] == "CL")
    expected = Decimal("4") + (Decimal("12") if year == 2026 else 0)
    assert Decimal(cl["balance"]) == expected and Decimal(cl["used"]) == Decimal("1")

    mine = client.get(f"{API}/me/leave-applications", headers=bearer(emp)).json()
    assert mine[0]["id"] == application_id
    assert client.post(f"{API}/me/leave-applications/{application_id}/cancel", headers=bearer(emp)).status_code == 409
    assert client.post(f"{API}/me/leave-applications/{application_id}/cancel",
                       headers=bearer(other)).status_code == 404

    cancelled = client.post(f"{API}/leave-applications/{application_id}/cancel", json={"comment": "x"}).json()
    assert cancelled["status"] == "CANCELLED"


def test_api_hr_records_backdated_leave(client, org):
    o = org
    day = date.today() - timedelta(days=7)
    day = next_working_day(o["db"], day)
    year = leave_year_for(o["db"], day)
    credit(o["db"], o["loner"], o["types"]["CL"], 3, year)
    response = client.post(f"{API}/leave-applications", json={
        "employee_id": o["loner"].id, "leave_type_id": o["types"]["CL"].id, "from_date": day.isoformat(),
        "to_date": day.isoformat(), "reason": "recorded", "comment": "by phone",
    })
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "APPROVED" and body["recorded_by_hr"] and body["step"] == "Recorded by HR"
    assert body["warnings"]
    assert client.get(f"{API}/leave-applications/{body['id']}").json()["hr_comment"] == "by phone"

    over = client.post(f"{API}/leave-applications", json={
        "employee_id": o["loner"].id, "leave_type_id": o["types"]["SL"].id, "from_date": day.isoformat(),
        "to_date": day.isoformat(),
    })
    assert over.status_code == 400  # overlaps (and no SL balance)


def test_employee_cannot_use_hr_leave_routes(client, org):
    emp = token_for(client, "emp")
    for path in ("/leave-applications", "/permissions", "/leave-balances", "/attendance-settings/policy"):
        assert client.get(f"{API}{path}", headers=bearer(emp)).status_code == 403, path
    assert client.get(f"{API}/me/leave-types", headers=bearer(emp)).status_code == 200
    # HR login without an employee record has no self-service.
    assert client.get(f"{API}/me/leave-applications").status_code == 403
