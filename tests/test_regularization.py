"""Missed-punch requests: rules, routing (manager then HR, MANAGER_ONLY),
the override that fills only the missing side, re-routing on a chain change
and the API."""
from datetime import date, datetime, time, timedelta

import pytest

from app.models.attendance import AttendanceDaily, AttendanceOverride
from app.models.leave import STATUS_APPROVED, STATUS_PENDING_HR, STATUS_PENDING_MANAGER, STATUS_REJECTED
from app.services import approval_settings
from app.services.attendance import monthly
from app.services.attendance.regularization import SOURCE_REGULARIZATION, apply_regularization, effective_day
from app.services.leave.calendar import ensure_default_policy
from app.services.leave.workflow import WorkflowError, hr_approve, hr_reject, manager_decide
from tests.leave_helpers import API, bearer, make_department, make_employee, make_user, seed_types, token_for

TODAY = date(2026, 11, 20)
D18, D19 = date(2026, 11, 18), date(2026, 11, 19)


@pytest.fixture()
def org(db_session):
    db = db_session
    seed_types(db)
    head = make_employee(db, "M1", "Meena")
    ops = make_department(db, "OPS", head)
    employee = make_employee(db, "E1", "Asha", department=ops, joined=date(2025, 1, 1))
    users = {"head": make_user(db, "head", head), "employee": make_user(db, "emp", employee),
             "hr": make_user(db, "hr2", role="HR")}
    return {"db": db, "head": head, "ops": ops, "employee": employee, "users": users}


def configure(db, **values):
    approval_settings.update_settings(db, values)
    db.commit()


def daily(db, employee, day, t_in="09:25", t_out=None):
    p_in = datetime.combine(day, time.fromisoformat(t_in)) if t_in else None
    p_out = datetime.combine(day, time.fromisoformat(t_out)) if t_out else None
    db.add(AttendanceDaily(employee_code=employee.employee_code, employee_id=employee.id, work_date=day,
                           punch_in=p_in, punch_out=p_out, punch_count=(1 if p_in else 0) + (1 if p_out else 0)))
    db.commit()


def request(o, day=D19, punch_in=None, punch_out=time(18, 30), reason_code="DEVICE_NOT_WORKING", reason=None,
            today=TODAY):
    return apply_regularization(o["db"], employee=o["employee"], user=o["users"]["employee"], day=day,
                                punch_in=punch_in, punch_out=punch_out, reason_code=reason_code, reason=reason,
                                today=today)


def active_override(db, employee, day):
    return db.query(AttendanceOverride).filter_by(employee_id=employee.id, work_date=day, is_active=True).first()


# --- apply rules -----------------------------------------------------------------------

def test_apply_routes_to_manager(org):
    daily(org["db"], org["employee"], D19)
    r = request(org)
    assert r.status == STATUS_PENDING_MANAGER and r.manager_id == org["head"].id


def test_future_date_and_back_limit(org):
    with pytest.raises(WorkflowError, match="future"):
        request(org, day=TODAY + timedelta(days=1))
    with pytest.raises(WorkflowError, match="last 7 day"):
        request(org, day=TODAY - timedelta(days=8))
    request(org, day=TODAY - timedelta(days=7))


def test_back_limit_follows_policy(org):
    policy = ensure_default_policy(org["db"])
    policy.regularization_max_back_days = 2
    org["db"].commit()
    with pytest.raises(WorkflowError, match="last 2 day"):
        request(org, day=TODAY - timedelta(days=3))


def test_monthly_cap(org):
    for day in (date(2026, 11, 14), date(2026, 11, 16), date(2026, 11, 17)):
        request(org, day=day)
    with pytest.raises(WorkflowError, match="Only 3 missed-punch"):
        request(org, day=D18)
    with pytest.raises(WorkflowError, match="Only 3"):
        request(org, day=D19)


def test_rejected_requests_do_not_count_towards_the_cap(org):
    first = request(org, day=date(2026, 11, 16))
    manager_decide(org["db"], first, org["users"]["head"], True, None)
    hr_reject(org["db"], first, org["users"]["hr"], "no")
    for day in (date(2026, 11, 17), D18, D19):
        request(org, day=day)


def test_one_open_request_per_day_and_input_checks(org):
    request(org)
    with pytest.raises(WorkflowError, match="already an open"):
        request(org, punch_in=time(9, 30), punch_out=None)
    with pytest.raises(WorkflowError, match="IN time, OUT time or both"):
        request(org, day=D18, punch_in=None, punch_out=None)
    with pytest.raises(WorkflowError, match="Reason must be"):
        request(org, day=D18, reason_code="BORED")
    with pytest.raises(WorkflowError, match="Describe the reason"):
        request(org, day=D18, reason_code="OTHER")
    with pytest.raises(WorkflowError, match="after the IN time"):
        request(org, day=D18, punch_in=time(18, 0), punch_out=time(9, 0))


def test_side_already_recorded_is_refused(org):
    daily(org["db"], org["employee"], D19, "09:25", "18:40")
    with pytest.raises(WorkflowError, match="OUT punch is already recorded"):
        request(org)
    daily(org["db"], org["employee"], D18, "10:00")
    with pytest.raises(WorkflowError, match="after the IN time"):
        request(org, day=D18, punch_out=time(9, 0))


# --- approval ----------------------------------------------------------------------------

def test_manager_then_hr_fills_only_the_missing_side(org):
    db = org["db"]
    daily(db, org["employee"], D19, "09:25")
    r = request(org)
    manager_decide(db, r, org["users"]["head"], True, "ok")
    assert r.status == STATUS_PENDING_HR
    assert active_override(db, org["employee"], D19) is None
    hr_approve(db, r, org["users"]["hr"], None)
    db.commit()
    assert r.status == STATUS_APPROVED and r.applied_sides == "OUT"
    row = active_override(db, org["employee"], D19)
    assert row.source == SOURCE_REGULARIZATION and row.id == r.override_id
    assert row.punch_in is None and row.punch_out == datetime(2026, 11, 19, 18, 30)
    eff = effective_day(db, org["employee"].id, D19)
    assert eff.punch_in == datetime(2026, 11, 19, 9, 25) and eff.punch_out == datetime(2026, 11, 19, 18, 30)


def test_both_sides_on_a_day_without_punches(org):
    db = org["db"]
    r = request(org, punch_in=time(9, 20), punch_out=time(18, 10))
    manager_decide(db, r, org["users"]["head"], True, None)
    hr_approve(db, r, org["users"]["hr"], None)
    assert r.applied_sides == "IN,OUT"
    eff = effective_day(db, org["employee"].id, D19)
    assert (eff.punch_in.time(), eff.punch_out.time()) == (time(9, 20), time(18, 10))


def test_side_that_arrived_meanwhile_is_not_overwritten(org):
    db = org["db"]
    r = request(org, punch_in=time(9, 0), punch_out=time(18, 30))
    daily(db, org["employee"], D19, "09:12")
    manager_decide(db, r, org["users"]["head"], True, None)
    hr_approve(db, r, org["users"]["hr"], None)
    assert r.applied_sides == "OUT"
    eff = effective_day(db, org["employee"].id, D19)
    assert eff.punch_in.time() == time(9, 12) and eff.punch_out.time() == time(18, 30)


def test_hr_status_correction_blocks_approval(org):
    db = org["db"]
    r = request(org)
    db.add(AttendanceOverride(employee_id=org["employee"].id, employee_code="E1", work_date=D19, kind="STATUS",
                              status_code="A", half="FULL", source="MANUAL", is_active=True))
    db.commit()
    manager_decide(db, r, org["users"]["head"], True, None)
    with pytest.raises(WorkflowError, match="HR has set the attendance"):
        hr_approve(db, r, org["users"]["hr"], None)


def test_manager_only_is_final_and_applies(org):
    db = org["db"]
    configure(db, regularization_chain="MANAGER_ONLY")
    daily(db, org["employee"], D19, "09:25")
    r = request(org)
    manager_decide(db, r, org["users"]["head"], True, None)
    assert r.status == STATUS_APPROVED and r.applied_sides == "OUT"
    assert active_override(db, org["employee"], D19).source == SOURCE_REGULARIZATION


def test_manager_reject(org):
    r = request(org)
    manager_decide(org["db"], r, org["users"]["head"], False, "no")
    assert r.status == STATUS_REJECTED and active_override(org["db"], org["employee"], D19) is None


def test_hr_only_chain_goes_to_hr(org):
    configure(org["db"], regularization_chain="HR_ONLY")
    r = request(org)
    assert r.status == STATUS_PENDING_HR and r.manager_id is None


def test_approval_clears_the_missing_out_lop(org):
    db = org["db"]
    policy = ensure_default_policy(db)
    policy.missing_out_policy = "ABSENT"
    db.commit()
    day = date(2026, 11, 16)
    daily(db, org["employee"], day, "09:25")
    before = monthly.resolve_month(db, org["employee"], "2026-11", today=TODAY)
    assert next(d for d in before.days if d.date == day).lop == 1
    r = request(org, day=day)
    manager_decide(db, r, org["users"]["head"], True, None)
    hr_approve(db, r, org["users"]["hr"], None)
    after = monthly.resolve_month(db, org["employee"], "2026-11", today=TODAY)
    assert next(d for d in after.days if d.date == day).lop == 0


# --- re-routing on a chain change ------------------------------------------------------------

def test_chain_change_reroutes_pending_hr_requests(client, org):
    db = org["db"]
    configure(db, regularization_chain="HR_ONLY")
    r = request(org)
    db.commit()
    assert r.status == STATUS_PENDING_HR
    body = client.get(f"{API}/settings/approvals").json()
    saved = client.put(f"{API}/settings/approvals", json={**body, "regularization_chain": "MANAGER_HR"})
    assert saved.status_code == 200, saved.text
    db.refresh(r)
    assert r.status == STATUS_PENDING_MANAGER and r.manager_id == org["head"].id


def test_chain_change_between_manager_chains_does_not_reroute(client, org):
    db = org["db"]
    org["ops"].manager_employee_id = None
    db.commit()
    r = request(org)
    assert r.status == STATUS_PENDING_HR
    org["ops"].manager_employee_id = org["head"].id
    db.commit()
    body = client.get(f"{API}/settings/approvals").json()
    client.put(f"{API}/settings/approvals", json={**body, "regularization_chain": "MANAGER_ONLY"})
    db.refresh(r)
    assert r.status == STATUS_PENDING_HR


# --- API ---------------------------------------------------------------------------------

def test_api_apply_manager_hr_and_counts(client, org, monkeypatch):
    import app.services.attendance.regularization as service
    from datetime import date as real_date

    class FakeDate(real_date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(service, "date", FakeDate)
    db = org["db"]
    daily(db, org["employee"], D19, "09:25")
    employee = bearer(token_for(client, "emp"))
    manager = bearer(token_for(client, "head"))

    rules = client.get(f"{API}/me/regularization-rules", headers=employee).json()
    assert rules["max_back_days"] == 7 and rules["monthly_cap"] == 3 and rules["has_manager"] is True
    day = client.get(f"{API}/me/regularization-day", params={"date": "2026-11-19"}, headers=employee).json()
    assert day["punch_in"].startswith("2026-11-19T09:25") and day["punch_out"] is None

    created = client.post(f"{API}/me/regularizations", headers=employee, json={
        "date": "2026-11-19", "punch_out": "18:30", "reason_code": "POWER_CUT"})
    assert created.status_code == 201, created.text
    rid = created.json()["id"]
    assert created.json()["reason_label"] == "Power cut"
    assert client.get(f"{API}/leave/pending-counts").json()["regularizations"] == 1

    inbox = client.get(f"{API}/me/approvals", headers=manager).json()
    assert [x["id"] for x in inbox["regularizations"]] == [rid]
    approved = client.post(f"{API}/me/approvals/regularization/{rid}/approve", headers=manager, json={})
    assert approved.status_code == 200 and approved.json()["status"] == STATUS_PENDING_HR

    queue = client.get(f"{API}/regularizations", params={"status": "PENDING"}).json()
    assert [x["id"] for x in queue] == [rid]
    done = client.post(f"{API}/regularizations/{rid}/approve", json={"comment": "ok"})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == STATUS_APPROVED and done.json()["applied_sides"] == "OUT"
    mine = client.get(f"{API}/me/regularizations", headers=employee).json()
    assert mine[0]["status"] == STATUS_APPROVED

    refused = client.post(f"{API}/me/regularizations", headers=employee, json={
        "date": "2026-11-19", "punch_out": "18:45", "reason_code": "FORGOT"})
    assert refused.status_code == 400 and "already recorded" in refused.json()["detail"]


def test_api_employee_cancel(client, org, monkeypatch):
    import app.services.attendance.regularization as service
    from datetime import date as real_date

    class FakeDate(real_date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(service, "date", FakeDate)
    employee = bearer(token_for(client, "emp"))
    rid = client.post(f"{API}/me/regularizations", headers=employee, json={
        "date": "2026-11-19", "punch_in": "09:30", "reason_code": "FORGOT"}).json()["id"]
    cancelled = client.post(f"{API}/me/regularizations/{rid}/cancel", headers=employee, json={})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"


def test_hr_cancel_of_approved_request_removes_the_correction(org):
    from app.services.attendance.regularization import cancel_by_hr

    db = org["db"]
    daily(db, org["employee"], D19, "09:25")
    r = request(org)
    manager_decide(db, r, org["users"]["head"], True, None)
    hr_approve(db, r, org["users"]["hr"], None)
    assert effective_day(db, org["employee"].id, D19).punch_out is not None
    cancel_by_hr(db, r, org["users"]["hr"], "wrong day")
    assert r.status == "CANCELLED"
    assert active_override(db, org["employee"], D19) is None
    assert effective_day(db, org["employee"].id, D19).punch_out is None


def test_approval_reprocesses_a_processed_month(org):
    from app.db.seed.pay_groups_seed import seed_pay_groups
    from app.models.attendance_month import AttendanceMonthResult
    from app.models.pay_group import EmployeePayProfile, PayGroup

    db = org["db"]
    seed_pay_groups(db)
    chennai = db.query(PayGroup).filter_by(code="CHENNAI_HO").one()
    db.add(EmployeePayProfile(employee_id=org["employee"].id, pay_group_id=chennai.id,
                              effective_from=date(2026, 1, 1), monthly_salary=30000))
    policy = ensure_default_policy(db)
    policy.missing_out_policy = "ABSENT"
    db.commit()
    day = date(2026, 11, 16)
    daily(db, org["employee"], day, "09:25")
    monthly.process_month(db, "2026-11", chennai.id, org["users"]["hr"], today=TODAY)
    db.commit()
    row = db.query(AttendanceMonthResult).filter_by(employee_id=org["employee"].id, month="2026-11").one()
    lop_before = row.lop_days
    r = request(org, day=day)
    manager_decide(db, r, org["users"]["head"], True, None)
    hr_approve(db, r, org["users"]["hr"], None)
    db.commit()
    db.refresh(row)
    assert row.lop_days == lop_before - 1
