from datetime import date
from decimal import Decimal

import pytest

from app.models.leave import ENTRY_CREDIT, ENTRY_LAPSE, LeaveLedger
from app.services.leave import scheduler as leave_scheduler
from app.services.leave.balance import (
    balance,
    available_on,
    balance_summaries,
    credit_catch_up,
    credit_month,
    tier_labels,
)
from app.services.leave.workflow import WorkflowError, apply_leave, cancel_by_hr, hr_approve
from tests.leave_helpers import API, make_employee, make_user, seed_types

TIERS = [
    {"min_service_months": 0, "days_per_month": 1, "carry_forward": False, "carry_forward_limit": None},
    {"min_service_months": 12, "days_per_month": 2, "carry_forward": True, "carry_forward_limit": None},
]
APR8, APR9 = date(2026, 4, 8), date(2026, 4, 9)  # Wed, Thu


@pytest.fixture()
def types(db_session):
    types = seed_types(db_session)
    types["CL"].credit_mode = "MONTHLY_TIERED"
    types["CL"].service_tiers = TIERS
    db_session.commit()
    return types


def run(db, year, month):
    stats = credit_month(db, year, month, as_of=date(year, month, 1))
    db.commit()
    return stats


def cl(db, types, employee, year=2026):
    return balance(db, employee.id, types["CL"].id, year)


def rows(db, employee, entry_type=None):
    query = db.query(LeaveLedger).filter(LeaveLedger.employee_id == employee.id)
    if entry_type:
        query = query.filter(LeaveLedger.entry_type == entry_type)
    return query.order_by(LeaveLedger.id).all()


def apply(db, types, employee, user, start, end=None, today=date(2026, 4, 1)):
    return apply_leave(db, employee=employee, user=user, leave_type_id=types["CL"].id, from_date=start,
                       from_half="FULL", to_date=end or start, to_half="FULL", reason="x", today=today)


def new_joiner(db, code="E1", joined=date(2026, 3, 10)):
    employee = make_employee(db, code, joined=joined)
    return employee


def test_under_one_year_gets_one_a_month_and_it_lapses(db_session, types):
    db = db_session
    employee = new_joiner(db)
    run(db, 2026, 3)
    assert cl(db, types, employee) == Decimal("1")
    run(db, 2026, 4)
    assert cl(db, types, employee) == Decimal("1")
    lapse = rows(db, employee, ENTRY_LAPSE)
    assert [(r.reference_type, r.reference_id, r.days) for r in lapse] == [("monthly_lapse", "2026-03", Decimal("-1"))]
    assert "lapsed" in lapse[0].note and "< 1 year: 1/month, lapses" in lapse[0].note
    summary = balance_summaries(db, 2026, [employee.id])[(employee.id, types["CL"].id)]
    assert summary["credited"] == Decimal("2") and summary["lapsed"] == Decimal("1")
    assert summary["balance"] == Decimal("1")


def test_in_month_use_then_cancel_after_lapse_relapses(db_session, types):
    db = db_session
    employee = new_joiner(db)
    user, hr = make_user(db, "emp", employee), make_user(db, "hr2", role="HR")
    run(db, 2026, 3)
    run(db, 2026, 4)
    application = apply(db, types, employee, user, APR8)
    hr_approve(db, application, hr, None)
    db.commit()
    assert cl(db, types, employee) == Decimal("0")

    run(db, 2026, 5)
    april = [r for r in rows(db, employee, ENTRY_LAPSE) if r.reference_id == "2026-04"]
    assert [r.days for r in april] == [Decimal("0")]  # fully used: marks April as lapsed
    assert cl(db, types, employee) == Decimal("1")

    cancel_by_hr(db, application, hr, "not needed")
    db.commit()
    assert cl(db, types, employee) == Decimal("1")  # April's restored day lapses again
    april = [r.days for r in rows(db, employee, ENTRY_LAPSE) if r.reference_id == "2026-04"]
    assert sum(april) == Decimal("-1")


def test_two_days_with_one_available_is_blocked_for_employee(db_session, types):
    db = db_session
    employee = new_joiner(db)
    user = make_user(db, "emp", employee)
    run(db, 2026, 3)
    run(db, 2026, 4)
    with pytest.raises(WorkflowError) as exc:
        apply(db, types, employee, user, APR8, APR9)
    assert "Insufficient" in exc.value.message and "1 day(s) available, 2 requested" in exc.value.message
    assert apply(db, types, employee, user, APR8).days == Decimal("1")
    with pytest.raises(WorkflowError):
        apply(db, types, employee, user, APR9)  # the pending day already uses April's credit


def test_backdated_leave_in_a_lapsed_month_uses_that_months_credit(db_session, types):
    db = db_session
    employee = new_joiner(db)
    run(db, 2026, 3)
    run(db, 2026, 4)
    db.add(LeaveLedger(employee_id=employee.id, leave_type_id=types["CL"].id, leave_year=2026,
                       entry_type="LEAVE_DEBIT", days=Decimal("-1"), reference_type="attendance_month",
                       reference_id="2026-03", note="attendance"))
    db.commit()
    assert cl(db, types, employee) == Decimal("1")  # April's credit is untouched
    run(db, 2026, 5)
    march = [r.days for r in rows(db, employee, ENTRY_LAPSE) if r.reference_id == "2026-03"]
    assert march == [Decimal("-1"), Decimal("1")]
    assert cl(db, types, employee) == Decimal("1")


def test_crossing_twelve_months_moves_to_two_and_starts_carrying(db_session, types):
    db = db_session
    employee = new_joiner(db, joined=date(2025, 11, 10))
    stats = credit_catch_up(db, 2026, as_of=date(2026, 12, 1))
    db.commit()
    assert len(stats["months"]) == 12
    credits = {r.reference_id: r.days for r in rows(db, employee, ENTRY_CREDIT)}
    assert credits["2026-11"] == Decimal("1")  # 11 completed months on 1 Nov
    assert credits["2026-12"] == Decimal("2")  # 12 completed months on 1 Dec
    assert {r.reference_id for r in rows(db, employee, ENTRY_LAPSE)} == {f"2026-{m:02d}" for m in range(1, 12)}
    assert cl(db, types, employee) == Decimal("2")

    run(db, 2027, 1)
    assert cl(db, types, employee, 2026) == Decimal("2")  # December is not lapsed
    assert cl(db, types, employee, 2027) == Decimal("4")  # 2 carried + 2 for January
    run(db, 2027, 2)
    assert cl(db, types, employee, 2027) == Decimal("6")


def test_no_carry_tier_lapses_at_year_end(db_session, types):
    db = db_session
    employee = new_joiner(db, joined=date(2026, 6, 1))
    credit_catch_up(db, 2026, as_of=date(2026, 12, 31))
    run(db, 2027, 1)
    assert cl(db, types, employee, 2026) == Decimal("0")
    assert cl(db, types, employee, 2027) == Decimal("1")
    assert not rows(db, employee, "CARRY_FORWARD")


@pytest.mark.parametrize("limit, expected", [(None, Decimal("24")), (5, Decimal("7"))])
def test_one_year_plus_accumulates_into_next_year(db_session, types, limit, expected):
    db = db_session
    types["CL"].service_tiers = [TIERS[0], {**TIERS[1], "carry_forward_limit": limit}]
    db.commit()
    veteran = new_joiner(db, joined=date(2020, 1, 1))
    for month in (1, 2, 3):
        run(db, 2026, month)
    assert cl(db, types, veteran) == Decimal("6")
    credit_catch_up(db, 2026, as_of=date(2026, 12, 31))
    db.commit()
    assert cl(db, types, veteran) == Decimal("24") and not rows(db, veteran, ENTRY_LAPSE)
    run(db, 2027, 1)
    carried = rows(db, veteran, "CARRY_FORWARD")
    assert [r.days for r in carried] == [expected - 2 if limit else Decimal("24")]
    assert cl(db, types, veteran, 2027) == (Decimal("26") if limit is None else expected)


def test_joining_cutoff_day(db_session, types):
    db = db_session
    on_cutoff = new_joiner(db, "E1", joined=date(2026, 3, 15))
    after = new_joiner(db, "E2", joined=date(2026, 3, 16))
    run(db, 2026, 3)
    assert cl(db, types, on_cutoff) == Decimal("1") and cl(db, types, after) == Decimal("0")
    assert "joined 15 Mar 2026" in rows(db, on_cutoff, ENTRY_CREDIT)[0].note
    types["CL"].joining_cutoff_day = 20
    db.commit()
    run(db, 2026, 3)
    assert cl(db, types, after) == Decimal("1")


def test_reruns_and_catch_up_are_idempotent(db_session, types):
    db = db_session
    joiner = new_joiner(db, "E1")
    veteran = new_joiner(db, "E2", joined=date(2019, 5, 20))
    no_date = new_joiner(db, "E3", joined=None)
    first = credit_catch_up(db, 2026, as_of=date(2026, 6, 30))
    db.commit()
    assert first["missing_joining_date"] == ["E3"] and not rows(db, no_date)
    count = db.query(LeaveLedger).count()
    run(db, 2026, 6)
    again = credit_catch_up(db, 2026, as_of=date(2026, 6, 30))
    assert again["credit_entries"] == again["lapse_entries"] == again["carry_forward_entries"] == 0
    assert db.query(LeaveLedger).count() == count
    assert cl(db, types, joiner) == Decimal("1") and cl(db, types, veteran) == Decimal("12")


def test_future_month_is_refused(db_session, types):
    with pytest.raises(ValueError):
        credit_month(db_session, 2026, 7, as_of=date(2026, 6, 30))


def test_tier_labels(db_session, types):
    db = db_session
    joiner, veteran = new_joiner(db, "E1"), new_joiner(db, "E2", joined=date(2020, 1, 1))
    labels = tier_labels([types["CL"], types["SL"]], [joiner, veteran], on=date(2026, 11, 17))
    assert labels == {(joiner.id, types["CL"].id): "< 1 year: 1/month, lapses",
                      (veteran.id, types["CL"].id): "1 year+: 2/month, carries forward"}


def test_credit_endpoints_and_balance_labels(client, db_session, types):
    db = db_session
    joiner = new_joiner(db, "E1", joined=date(2025, 6, 1))
    veteran = new_joiner(db, "E2", joined=date(2020, 1, 1))
    month = client.post(f"{API}/leave-balances/credit-month", json={"year": 2025, "month": 6})
    assert month.status_code == 200, month.text
    assert month.json()["credit_entries"] == 2
    assert client.post(f"{API}/leave-balances/credit-month", json={"year": 2100, "month": 1}).status_code == 400

    caught = client.post(f"{API}/leave-balances/credit-catch-up", json={"year": 2025})
    assert caught.status_code == 200, caught.text
    body = caught.json()
    assert len(body["months"]) == 12 and body["lapse_entries"] == 6  # June..November for the joiner
    listed = client.get(f"{API}/leave-balances", params={"year": 2025}).json()
    by_code = {r["employee_code"]: {b["leave_type_code"]: b for b in r["balances"]} for r in listed}
    assert Decimal(by_code["E1"]["CL"]["balance"]) == Decimal("1")
    assert Decimal(by_code["E1"]["CL"]["lapsed"]) == Decimal("6")
    assert Decimal(by_code["E2"]["CL"]["balance"]) == Decimal("24")
    assert by_code["E2"]["CL"]["tier_label"] == "1 year+: 2/month, carries forward"
    assert by_code["E2"]["SL"]["tier_label"] is None
    ledger = client.get(f"{API}/leave-balances/ledger", params={"employee_id": joiner.id, "year": 2025}).json()
    assert sum(1 for e in ledger if e["entry_type"] == "LAPSE") == 6


def test_leave_type_api_tiers(client, types):
    payload = {"code": "CL2", "name": "Casual 2", "credit_mode": "MONTHLY_TIERED", "joining_cutoff_day": 10,
               "service_tiers": [TIERS[1], {**TIERS[0], "carry_forward_limit": 3}]}
    created = client.post(f"{API}/leave-types", json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert [t["min_service_months"] for t in body["service_tiers"]] == [0, 12]
    assert body["service_tiers"][0]["carry_forward_limit"] is None  # no limit without carry forward
    assert body["joining_cutoff_day"] == 10
    bad = {**payload, "code": "CL3", "service_tiers": []}
    assert client.post(f"{API}/leave-types", json=bad).status_code == 422
    dup = {**payload, "code": "CL4", "service_tiers": [TIERS[0], TIERS[0]]}
    assert client.post(f"{API}/leave-types", json=dup).status_code == 422


def test_scheduler_is_off_in_tests_and_job_runs_monthly():
    assert leave_scheduler.start_leave_scheduler() is None
    assert not leave_scheduler.is_running()
    scheduler = leave_scheduler.build_scheduler()
    job = scheduler.get_job(leave_scheduler.JOB_ID)
    fields = {f.name: str(f) for f in job.trigger.fields}
    assert (fields["day"], fields["hour"], fields["minute"]) == ("1", "0", "30")


def test_available_on_is_month_aware_for_the_no_carry_tier(db_session, types):
    db = db_session
    joiner, veteran = new_joiner(db, "E1"), new_joiner(db, "E2", joined=date(2020, 1, 1))
    run(db, 2026, 3)
    run(db, 2026, 4)
    cl_id = types["CL"].id
    assert available_on(db, joiner.id, cl_id, APR8) == Decimal("1")
    assert available_on(db, joiner.id, cl_id, date(2026, 3, 20)) == Decimal("1")  # March's lapsed credit
    assert available_on(db, joiner.id, cl_id, date(2026, 5, 6)) == Decimal("1")  # May, not credited yet
    db.add(LeaveLedger(employee_id=joiner.id, leave_type_id=cl_id, leave_year=2026, entry_type="LEAVE_DEBIT",
                       days=Decimal("-1"), reference_type="attendance_month", reference_id="2026-04"))
    db.commit()
    assert available_on(db, joiner.id, cl_id, APR8) == Decimal("0")
    assert available_on(db, joiner.id, cl_id, date(2026, 5, 6)) == Decimal("1")
    assert available_on(db, veteran.id, cl_id, APR8) == Decimal("4")  # carry tier: the year balance


def test_under_one_year_can_apply_ahead_for_next_months_credit(db_session, types):
    db = db_session
    employee = new_joiner(db)
    user = make_user(db, "emp_ahead", employee)
    run(db, 2026, 3)
    run(db, 2026, 4)
    apply(db, types, employee, user, APR8)
    with pytest.raises(WorkflowError):
        apply(db, types, employee, user, APR9)
    app = apply(db, types, employee, user, date(2026, 5, 6), today=date(2026, 4, 20))
    assert app.days == Decimal("1")
