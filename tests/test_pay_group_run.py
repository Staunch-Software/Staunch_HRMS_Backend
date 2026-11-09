"""Payroll-run integration for pay groups: a run with pay_group_id is scoped
to the group's employees (profile in force on period_end, payable status)
and each line item is computed by the group's strategy and persisted like
any other line item.

CCTV group, seeded params, Skilled, day wage 1000:
  E-R1 (24 days present, loan 500):
    wages 24000 + allowance 144.73*24 = 3473.52 + bonus MIN(1999.2, 1749.3) = 1749.3
    + transport 34.73*24 = 833.52 = 30056.34; PF: 24000 <= 25000 -> 2880
    net = ROUNDUP(30056.34 - 2880 - 500) = ROUNDUP(26676.34) = 26677
    round-off 0.66 -> gross 30057.00, deductions 3380.00
  E-R2 (no variable input -> 26 standard days, note added):
    26000 + 3762.98 + 1749.3 + 902.98 = 32415.26; 26000 > 25000 -> PF 3000
    net = ROUNDUP(29415.26) = 29416; gross 32416.00, deductions 3000.00
  E-R6 (profile with an unknown skill, inserted directly) -> Error line item
Out of scope: E-R3 (MCBS), E-R4 (Inactive), E-R5 (left CCTV on 31 Aug for MCBS).
"""
from datetime import date

from app.db.seed.pay_groups_seed import seed_pay_groups
from app.models.employee import Employee
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun
from app.models.variable_pay import VariablePayInput
from app.services.payroll_engine.engine import _resolve_employee_scope


def _employee(db, code, status="Active"):
    employee = Employee(employee_code=code, first_name=code, status=status, date_of_joining=date(2025, 1, 1))
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return employee


def _profile(client, employee, group, effective_from="2026-01-01", **extra):
    body = {"pay_group_id": group.id, "daily_wage": 1000, "skill_level": "Skilled",
            "effective_from": effective_from, **extra}
    resp = client.put(f"/api/v1/employees/{employee.id}/pay-profile", json=body)
    assert resp.status_code == 200, resp.text


def _setup(client, db):
    seed_pay_groups(db)
    groups = {g.code: g for g in db.query(PayGroup).all()}
    cctv, mcbs = groups["CCTV"], groups["MCBS"]

    r1, r2, r3 = _employee(db, "E-R1"), _employee(db, "E-R2"), _employee(db, "E-R3")
    r4, r5, r6 = _employee(db, "E-R4", status="Inactive"), _employee(db, "E-R5"), _employee(db, "E-R6")
    _profile(client, r1, cctv)
    _profile(client, r2, cctv)
    _profile(client, r3, mcbs)
    _profile(client, r4, cctv)
    _profile(client, r5, cctv)
    _profile(client, r5, mcbs, effective_from="2026-09-01")
    db.add(EmployeePayProfile(employee_id=r6.id, pay_group_id=cctv.id, daily_wage=1000, skill_level="Unskilled",
                              extra={}, effective_from=date(2026, 1, 1)))
    db.add(VariablePayInput(employee_id=r1.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
                            present_days=24, lop_days=2, overtime_hours=0,
                            ad_hoc_earnings=[], ad_hoc_deductions=[{"component_code": "LOAN", "amount": 500}]))
    schedule = PaySchedule(schedule_name="Monthly", pay_frequency="Monthly", payment_day_rule={"type": "LastDayOfMonth"},
                           holiday_adjustment="Before", is_active=True, applies_to=None)
    db.add(schedule)
    db.commit()
    return cctv, schedule, (r1, r2, r6)


def test_pay_group_run_scope_and_line_items(client, db_session):
    cctv, schedule, (r1, r2, r6) = _setup(client, db_session)
    resp = client.post("/api/v1/payroll-runs", json={
        "pay_schedule_id": schedule.id, "pay_group_id": cctv.id, "period_start": "2026-09-01",
        "period_end": "2026-09-30", "pay_date": "2026-09-30",
    })
    assert resp.status_code == 201, resp.text
    run_id = resp.json()["id"]
    assert resp.json()["pay_group_id"] == cctv.id

    run = db_session.get(PayrollRun, run_id)
    assert [e.employee_code for e in _resolve_employee_scope(db_session, run)] == ["E-R1", "E-R2", "E-R6"]

    calc = client.post(f"/api/v1/payroll-runs/{run_id}/calculate")
    assert calc.status_code == 200, calc.text
    body = calc.json()
    assert body["status"] == "Calculated"
    assert body["employee_count"] == 3
    assert body["total_net"] == 26677 + 29416
    assert body["total_gross"] == 30057 + 32416
    assert body["total_deductions"] == 3380 + 3000

    items = {i["employee_id"]: i for i in client.get(f"/api/v1/payroll-runs/{run_id}/line-items").json()}
    one, two, six = items[r1.id], items[r2.id], items[r6.id]
    assert (one["gross_earnings"], one["total_deductions"], one["net_pay"]) == (30057, 3380, 26677)
    assert one["paid_days"] == 24 and one["pf_wages"] == 24000
    assert one["status"] == "Included"
    assert one["employee_snapshot"]["employee_code"] == "E-R1"
    assert one["employee_snapshot"]["pay_group_code"] == "CCTV"
    codes = [entry["component_code"] for entry in one["computation_breakdown"]]
    assert codes[0] == "_PAY_GROUP"
    assert {"WAGES", "ALLOWANCE", "BONUS", "TRANSPORT", "PF_EE", "LOAN", "ROUND_OFF"} <= set(codes)

    assert two["net_pay"] == 29416 and two["paid_days"] == 26
    assert any("No variable input" in (e.get("warning") or "") for e in two["computation_breakdown"])

    assert six["status"] == "Error"
    assert "skill level" in six["error_message"]

    approve = client.post(f"/api/v1/payroll-runs/{run_id}/approve", json={"approved_by": "tester"})
    assert approve.status_code == 200 and approve.json()["status"] == "Approved"


def test_run_creation_validates_pay_group(client, db_session):
    cctv, schedule, _ = _setup(client, db_session)
    base = {"pay_schedule_id": schedule.id, "period_start": "2026-09-01", "period_end": "2026-09-30",
            "pay_date": "2026-09-30"}
    assert client.post("/api/v1/payroll-runs", json={**base, "pay_group_id": 9999}).status_code == 404
    cctv.is_active = False
    db_session.commit()
    assert client.post("/api/v1/payroll-runs", json={**base, "pay_group_id": cctv.id}).status_code == 400
    plain = client.post("/api/v1/payroll-runs", json=base)
    assert plain.status_code == 201 and plain.json()["pay_group_id"] is None
