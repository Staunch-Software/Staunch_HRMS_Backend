from datetime import date
from decimal import Decimal

import pytest

from app.models.leave import LeaveLedger
from app.services.leave.balance import allocate_from_balances, balance, credit_year
from tests.leave_helpers import API, credit, make_employee, seed_types


@pytest.fixture()
def types(db_session):
    return seed_types(db_session, cl=12, sl=10, el=15)


def bal(db, employee, leave_type, year=2027):
    return balance(db, employee.id, leave_type.id, year)


def test_upfront_credit_full_and_pro_rata(db_session, types):
    db = db_session
    veteran = make_employee(db, "E1", joined=date(2020, 4, 1))
    july = make_employee(db, "E2", joined=date(2027, 7, 10))
    august = make_employee(db, "E3", joined=date(2027, 8, 20))
    unknown = make_employee(db, "E4")
    future = make_employee(db, "E5", joined=date(2028, 1, 5))
    inactive = make_employee(db, "E6", status="Inactive", joined=date(2020, 1, 1))
    types["SL"].pro_rata_for_joiners = False
    db.commit()

    stats = credit_year(db, 2027, leave_type_ids=[types["CL"].id, types["SL"].id], as_of=date(2027, 1, 1))
    assert stats["employees"] == 4
    assert bal(db, veteran, types["CL"]) == Decimal("12") and bal(db, unknown, types["CL"]) == Decimal("12")
    assert bal(db, july, types["CL"]) == Decimal("6")  # Jul..Dec = 6 of 12 months
    assert bal(db, august, types["CL"]) == Decimal("5")  # 12 * 5 / 12
    assert bal(db, august, types["SL"]) == Decimal("10")  # no pro-rata for SL
    assert bal(db, future, types["CL"]) == Decimal("0")
    assert bal(db, inactive, types["CL"]) == Decimal("0")

    # Re-running writes nothing new.
    again = credit_year(db, 2027, leave_type_ids=[types["CL"].id, types["SL"].id])
    assert again["credit_entries"] == 0
    assert bal(db, veteran, types["CL"]) == Decimal("12")


def test_pro_rata_rounds_to_half_days(db_session, types):
    db = db_session
    types["SL"].annual_entitlement = Decimal("10")
    joiner = make_employee(db, "E1", joined=date(2027, 8, 1))
    credit_year(db, 2027, leave_type_ids=[types["SL"].id])
    assert bal(db, joiner, types["SL"]) == Decimal("4")  # 10 * 5 / 12 = 4.17 -> 4.0


def test_monthly_accrual_is_idempotent(db_session, types):
    db = db_session
    types["EL"].credit_mode = "MONTHLY_ACCRUAL"
    types["EL"].carry_forward = False
    types["SL"].credit_mode = "MONTHLY_ACCRUAL"
    db.commit()
    veteran = make_employee(db, "E1", joined=date(2020, 1, 1))
    joiner = make_employee(db, "E2", joined=date(2027, 2, 15))
    ids = [types["EL"].id, types["SL"].id]

    credit_year(db, 2027, leave_type_ids=ids, as_of=date(2027, 3, 10))
    assert bal(db, veteran, types["EL"]) == Decimal("3.75")
    assert bal(db, joiner, types["EL"]) == Decimal("2.50")  # Feb + Mar
    assert credit_year(db, 2027, leave_type_ids=ids, as_of=date(2027, 3, 31))["credit_entries"] == 0

    credit_year(db, 2027, leave_type_ids=ids, as_of=date(2027, 12, 31))
    assert bal(db, veteran, types["EL"]) == Decimal("15")
    assert bal(db, veteran, types["SL"]) == Decimal("10")  # 0.83/0.84 split sums exactly
    refs = [r for (r,) in db.query(LeaveLedger.reference_id).filter(
        LeaveLedger.employee_id == veteran.id, LeaveLedger.leave_type_id == types["EL"].id)]
    assert len(refs) == 12 and len(set(refs)) == 12


def test_carry_forward_capped(db_session, types):
    db = db_session
    types["EL"].carry_forward_limit = Decimal("5")
    db.commit()
    rich = make_employee(db, "E1", joined=date(2020, 1, 1))
    poor = make_employee(db, "E2", joined=date(2020, 1, 1))
    small = make_employee(db, "E3", joined=date(2020, 1, 1))
    credit(db, rich, types["EL"], 8, 2026)
    credit(db, poor, types["EL"], -2, 2026)
    credit(db, small, types["EL"], 3, 2026)

    stats = credit_year(db, 2027, leave_type_ids=[types["EL"].id])
    assert stats["carry_forward_entries"] == 2
    assert bal(db, rich, types["EL"]) == Decimal("20")  # 5 carried + 15
    assert bal(db, poor, types["EL"]) == Decimal("15")
    assert bal(db, small, types["EL"]) == Decimal("18")
    credit_year(db, 2027, leave_type_ids=[types["EL"].id])
    assert bal(db, rich, types["EL"]) == Decimal("20")

    types["EL"].carry_forward_limit = None
    credit(db, rich, types["EL"], 40, 2027)
    credit_year(db, 2028, leave_type_ids=[types["EL"].id])
    assert balance(db, rich.id, types["EL"].id, 2028) == Decimal("75")


def test_allocate_from_balances(db_session, types):
    db = db_session
    employee = make_employee(db, "E1")
    credit(db, employee, types["CL"], 0.5, 2027)
    credit(db, employee, types["EL"], 3, 2027)
    allocations, lop = allocate_from_balances(db, employee.id, 2027, Decimal("1.5"), ["CL", "EL"])
    assert [(t.code, d) for t, d in allocations] == [("CL", Decimal("0.5")), ("EL", Decimal("1"))]
    assert lop == 0
    allocations, lop = allocate_from_balances(db, employee.id, 2027, Decimal("5"), ["CL", "EL"])
    assert lop == Decimal("1.5")


def test_balance_api_adjust_and_credit_run(client, db_session, types):
    db = db_session
    employee = make_employee(db, "E1", first="Asha", joined=date(2020, 1, 1))
    make_employee(db, "E2", first="Gone", status="Inactive")

    result = client.post(f"{API}/leave-balances/credit-run", json={"year": 2027, "as_of": "2027-01-01"})
    assert result.status_code == 200, result.text
    assert result.json()["credit_entries"] == 3  # CL, SL, EL upfront for one active employee

    rows = client.get(f"{API}/leave-balances", params={"year": 2027}).json()
    assert [r["employee_code"] for r in rows] == ["E1"]
    by_code = {b["leave_type_code"]: b for b in rows[0]["balances"]}
    assert set(by_code) == {"CL", "SL", "EL"}
    assert Decimal(by_code["CL"]["balance"]) == Decimal("12")

    adjusted = client.post(f"{API}/leave-balances/adjust", json={
        "employee_id": employee.id, "leave_type_id": types["CL"].id, "year": 2027, "days": -2, "note": "Correction",
    })
    assert adjusted.status_code == 200, adjusted.text
    cl = next(b for b in adjusted.json()["balances"] if b["leave_type_code"] == "CL")
    assert Decimal(cl["balance"]) == Decimal("10") and Decimal(cl["adjusted"]) == Decimal("-2")

    assert client.post(f"{API}/leave-balances/adjust", json={
        "employee_id": employee.id, "leave_type_id": types["LOP"].id, "days": 1, "note": "x",
    }).status_code == 400
    assert client.post(f"{API}/leave-balances/adjust", json={
        "employee_id": employee.id, "leave_type_id": types["CL"].id, "days": 0, "note": "x",
    }).status_code == 422

    ledger = client.get(f"{API}/leave-balances/ledger", params={"employee_id": employee.id, "year": 2027}).json()
    assert sorted(e["entry_type"] for e in ledger) == ["ADJUSTMENT", "CREDIT", "CREDIT", "CREDIT"]


def test_leave_types_api(client, types):
    listed = client.get(f"{API}/leave-types").json()
    assert [t["code"] for t in listed] == ["CL", "SL", "EL", "LOP"]
    created = client.post(f"{API}/leave-types", json={
        "code": "ml", "name": "Maternity Leave", "annual_entitlement": 182, "sandwich_rule": False,
        "allowed_during_notice": True, "sort_order": 9,
    })
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["code"] == "ML" and body["credit_mode"] == "YEARLY_UPFRONT"
    assert client.post(f"{API}/leave-types", json={"code": "ML", "name": "Dup"}).status_code == 409
    assert client.post(f"{API}/leave-types", json={"code": "X", "name": "Bad", "credit_mode": "WEEKLY"}).status_code == 422

    updated = client.put(f"{API}/leave-types/{body['id']}", json={**body, "name": "Maternity", "is_active": False})
    assert updated.status_code == 200 and updated.json()["name"] == "Maternity"
    assert client.delete(f"{API}/leave-types/{body['id']}").status_code == 200
    assert client.delete(f"{API}/leave-types/{types['CL'].id}").status_code == 200
