"""Balances: the selected month's credit, usage and balance next to the
year totals, and the opening carry forward (reset to 0, entered by HR)."""
import importlib.util
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.leave import LeaveLedger
from app.services.leave.balance import available_on, balance_summaries, credit_catch_up
from app.services.leave.workflow import apply_leave
from tests.leave_helpers import API, bearer, make_employee, make_user, seed_types, token_for

TIERS = [
    {"min_service_months": 0, "days_per_month": 1, "carry_forward": False, "carry_forward_limit": None},
    {"min_service_months": 12, "days_per_month": 2, "carry_forward": True, "carry_forward_limit": None},
]
MIGRATION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "ab08ed3cb50d_approval_settings_maker_checker.py"


@pytest.fixture()
def setup(db_session):
    db = db_session
    types = seed_types(db)
    types["CL"].credit_mode = "MONTHLY_TIERED"
    types["CL"].service_tiers = TIERS
    db.commit()
    junior = make_employee(db, "J1", "Junior", joined=date(2026, 3, 10))
    senior = make_employee(db, "S1", "Senior", joined=date(2024, 1, 1))
    credit_catch_up(db, 2026, as_of=date(2026, 11, 1), leave_type_ids=[types["CL"].id])
    db.commit()
    return {"db": db, "types": types, "junior": junior, "senior": senior}


def cl_row(payload, code):
    employee = next(e for e in payload if e["employee_code"] == code)
    return next(b for b in employee["balances"] if b["leave_type_code"] == "CL"), employee


def test_month_fields_under_and_over_one_year(client, setup):
    s = setup
    user = make_user(s["db"], "jun", s["junior"])
    application = apply_leave(s["db"], employee=s["junior"], user=user, leave_type_id=s["types"]["CL"].id,
                              from_date=date(2026, 11, 9), from_half="FULL", to_date=date(2026, 11, 9),
                              to_half="FULL", reason=None, today=date(2026, 11, 2))
    s["db"].commit()
    assert application.status == "PENDING_HR"

    payload = client.get(f"{API}/leave-balances?year=2026&month=11").json()
    junior, employee = cl_row(payload, "J1")
    assert employee["month"] == "2026-11" and junior["month"] == "2026-11"
    assert Decimal(junior["month_credited"]) == 1 and Decimal(junior["month_pending"]) == 1
    assert Decimal(junior["month_available"]) == 0
    assert Decimal(junior["month_available"]) == available_on(
        s["db"], s["junior"].id, s["types"]["CL"].id, date(2026, 11, 1))
    assert Decimal(junior["balance"]) == 1 and Decimal(junior["carried_forward"]) == 0

    senior, _ = cl_row(payload, "S1")
    assert Decimal(senior["month_credited"]) == 2 and Decimal(senior["month_used"]) == 0
    assert Decimal(senior["month_available"]) == 2
    assert Decimal(senior["balance"]) == 22 and Decimal(senior["available"]) == 22

    october, _ = cl_row(client.get(f"{API}/leave-balances?year=2026&month=10").json(), "J1")
    assert october["month"] == "2026-10" and Decimal(october["month_pending"]) == 0
    assert client.get(f"{API}/leave-balances?year=2026&month=13").status_code == 422


def test_me_balances_month_split(client, setup):
    s = setup
    make_user(s["db"], "sen", s["senior"])
    body = client.get(f"{API}/me/balances?year=2026&month=11", headers=bearer(token_for(client, "sen"))).json()
    assert body["month"] == "2026-11"
    row = next(b for b in body["balances"] if b["leave_type_code"] == "CL")
    assert Decimal(row["month_credited"]) == 2 and Decimal(row["balance"]) == 22


def _migration():
    spec = importlib.util.spec_from_file_location("approval_migration", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_carry_forward_reset_to_zero(setup):
    s = setup
    db, senior, cl = s["db"], s["senior"], s["types"]["CL"]
    db.add(LeaveLedger(employee_id=senior.id, leave_type_id=cl.id, leave_year=2026, entry_type="CARRY_FORWARD",
                       days=Decimal("5"), reference_type="carry_forward", reference_id="2026",
                       note="Carried forward from 2025"))
    db.commit()
    assert balance_summaries(db, 2026, [senior.id])[(senior.id, cl.id)]["carried_forward"] == 5
    migration = _migration()
    assert migration.reset_carry_forward(db.connection(), 2026) == 1
    db.commit()
    summary = balance_summaries(db, 2026, [senior.id])[(senior.id, cl.id)]
    assert summary["carried_forward"] == 0 and summary["balance"] == 22
    reset = db.query(LeaveLedger).filter(LeaveLedger.reference_type == "carry_forward_reset").one()
    assert reset.note == "Opening carry forward set to 0" and reset.days == Decimal("-5")
    assert migration.reset_carry_forward(db.connection(), 2026) == 0


def test_opening_carry_forward_through_adjust(client, setup):
    s = setup
    response = client.post(f"{API}/leave-balances/adjust", json={
        "employee_id": s["senior"].id, "leave_type_id": s["types"]["CL"].id, "year": 2026, "days": 3,
        "note": "Opening balance from old system", "entry_type": "CARRY_FORWARD",
    })
    assert response.status_code == 200, response.text
    row = next(b for b in response.json()["balances"] if b["leave_type_code"] == "CL")
    assert Decimal(row["carried_forward"]) == 3 and Decimal(row["adjusted"]) == 0
    entry = s["db"].query(LeaveLedger).filter(LeaveLedger.entry_type == "CARRY_FORWARD").one()
    assert (entry.reference_type, entry.reference_id) == ("opening_carry_forward", "2026")
    bad = client.post(f"{API}/leave-balances/adjust", json={
        "employee_id": s["senior"].id, "leave_type_id": s["types"]["CL"].id, "days": 1, "note": "x",
        "entry_type": "LAPSE",
    })
    assert bad.status_code == 422
