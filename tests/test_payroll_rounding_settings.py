"""Rounding Rules: settings API, TDS to the nearest Rs 10, attendance day
step, defaults for new pay groups / lines and "apply to all groups"."""
from datetime import date
from decimal import Decimal

import pytest

from app.db.seed.pay_groups_seed import seed_pay_groups
from app.db.seed.statutory_seed import seed_tds
from app.models.pay_group import PayGroup, PayGroupDefinitionVersion
from app.models.payroll_settings import PayrollRoundingSettings
from app.services.attendance import monthly
from app.services.leave.calendar import ensure_default_policy
from app.services.payroll_engine.pay_groups.base import EARNING, PayLine, settle_net_pay
from app.services.payroll_engine.statutory.tds import compute_monthly_tds
from app.services.payroll_settings import round_days, round_tds
from tests.test_attendance_monthly import NOV, TODAY, _lates
from tests.leave_helpers import make_employee, seed_types
from tests.test_tds import _employee, _past_run, _schedule

API = "/api/v1/settings/rounding"


def _set(db, **values):
    row = db.query(PayrollRoundingSettings).first() or PayrollRoundingSettings()
    for key, value in {"default_net_rounding": "ROUND", "default_line_rounding": "ROUND", "default_line_decimals": 0,
                       "tds_rounding": "NEAREST_RUPEE", "attendance_day_step": "0.5", **values}.items():
        setattr(row, key, value)
    db.add(row)
    db.commit()


def test_defaults_and_update(client):
    body = client.get(API).json()
    assert body["default_net_rounding"] == "ROUND"
    assert body["tds_rounding"] == "NEAREST_RUPEE"
    assert body["attendance_day_step"] == "0.5"
    assert body["pay_groups"] == []

    payload = {"default_net_rounding": "ROUNDDOWN", "default_line_rounding": "ROUNDUP", "default_line_decimals": 2,
               "tds_rounding": "NEAREST_10", "attendance_day_step": "0.25"}
    response = client.put(API, json=payload)
    assert response.status_code == 200, response.text
    assert {k: response.json()[k] for k in payload} == payload
    assert client.put(API, json={**payload, "tds_rounding": "NEAREST_100"}).status_code == 422


def test_round_helpers():
    assert round_tds(Decimal("13133.33"), "NEAREST_10") == Decimal("13130")
    assert round_tds(Decimal("13135"), "NEAREST_10") == Decimal("13140")
    assert round_tds(Decimal("13133.5"), "NEAREST_RUPEE") == Decimal("13134")
    assert round_days(Decimal("0.3"), "0.25") == Decimal("0.25")
    assert round_days(Decimal("0.25"), "0.5") == Decimal("0.5")
    assert round_days(Decimal("1.2"), "0.5") == Decimal("1")
    assert round_days(Decimal("0.3"), "NONE") == Decimal("0.3")


def _tds_setup(db):
    seed_tds(db)
    employee = _employee(db)
    schedule = _schedule(db)
    for period_end in (date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 30),
                       date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30)):
        _past_run(db, schedule, employee, period_end, "Approved", 150000, 12000)
    return employee


def test_tds_nearest_rupee_by_default(db_session):
    employee = _tds_setup(db_session)
    result = compute_monthly_tds(db_session, employee, date(2026, 10, 31), Decimal("150000"))
    assert result["amount"] == Decimal("13133")


def test_tds_rounded_to_nearest_10(db_session):
    employee = _tds_setup(db_session)
    _set(db_session, tds_rounding="NEAREST_10")
    result = compute_monthly_tds(db_session, employee, date(2026, 10, 31), Decimal("150000"))
    # (1,50,800 - 72,000) / 6 = 13,133.33 -> 13,130
    assert result["annual_tax"] == Decimal("150800")
    assert result["amount"] == Decimal("13130")
    assert result["amount"] % 10 == 0


@pytest.fixture()
def quarter_day_late(db_session):
    seed_types(db_session)
    policy = ensure_default_policy(db_session)
    policy.late_deduction_days = Decimal("0.25")
    db_session.commit()
    emp = make_employee(db_session, "ST0001", joined=date(2025, 1, 1))
    _lates(db_session, emp, 3)
    return emp


def test_attendance_day_step_half_rounds_quarter_up(db_session, quarter_day_late):
    result = monthly.resolve_month(db_session, quarter_day_late, NOV, today=TODAY)
    assert Decimal(result.totals["late_lop"]) == Decimal("0.25")
    assert Decimal(result.totals["total_lop"]) == Decimal("0.5")
    assert result.lop_days == Decimal("0.5")


def test_attendance_day_step_quarter(db_session, quarter_day_late):
    _set(db_session, attendance_day_step="0.25")
    result = monthly.resolve_month(db_session, quarter_day_late, NOV, today=TODAY)
    assert Decimal(result.totals["total_lop"]) == Decimal("0.25")
    assert Decimal(result.totals["payroll_present_days"]) == Decimal("29.75")


def test_net_rounddown():
    lines = [PayLine("BASIC", "Basic", EARNING, Decimal("1000.75"), "x")]
    gross, deductions, net = settle_net_pay(lines, "ROUNDDOWN")
    assert net == Decimal("1000")
    assert lines[-1].amount == Decimal("-0.75")


def test_new_group_and_builder_defaults(client, db_session):
    _set(db_session, default_net_rounding="ROUNDUP", default_line_rounding="ROUNDDOWN", default_line_decimals=2)
    reference = client.get("/api/v1/pay-groups/formula-reference").json()
    assert reference["rounding_defaults"] == {"net_rounding": "ROUNDUP", "line_rounding": "ROUNDDOWN",
                                              "line_decimals": 2}
    response = client.post("/api/v1/pay-groups", json={"code": "NEWGRP", "name": "New group"})
    assert response.status_code == 201, response.text
    group = db_session.query(PayGroup).filter(PayGroup.code == "NEWGRP").one()
    assert group.definition["net_rounding"] == "ROUNDUP"
    assert group.definition["lines"][0]["rounding"] == "ROUNDDOWN"
    assert group.definition["lines"][0]["decimals"] == 2


def test_apply_net_rounding_creates_new_versions(client, db_session):
    seed_pay_groups(db_session)
    groups = db_session.query(PayGroup).order_by(PayGroup.code).all()
    before = {g.code: (g.definition_version, g.definition.get("net_rounding")) for g in groups}
    history_before = {(v.pay_group_id, v.version): dict(v.definition)
                      for v in db_session.query(PayGroupDefinitionVersion).all()}

    response = client.post(f"{API}/apply-net-rounding", json={"net_rounding": "ROUNDDOWN"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["net_rounding"] == "ROUNDDOWN"
    changed = {row["code"] for row in body["updated"]}
    assert changed == {code for code, (_, mode) in before.items() if mode != "ROUNDDOWN"}

    db_session.expire_all()
    for group in db_session.query(PayGroup).all():
        assert group.definition["net_rounding"] == "ROUNDDOWN"
        if group.code in changed:
            assert group.definition_version == before[group.code][0] + 1
    for (group_id, version), definition in history_before.items():
        row = db_session.query(PayGroupDefinitionVersion).filter_by(pay_group_id=group_id, version=version).one()
        assert row.definition == definition

    listing = client.get(API).json()["pay_groups"]
    assert {row["net_rounding"] for row in listing} == {"ROUNDDOWN"}
    again = client.post(f"{API}/apply-net-rounding", json={"net_rounding": "ROUNDDOWN"}).json()
    assert again["updated"] == [] and len(again["unchanged"]) == len(groups)


def test_rounding_settings_hr_only(client, db_session):
    from tests.leave_helpers import bearer, make_user, token_for

    emp = make_employee(db_session, "E1")
    make_user(db_session, "emp1", emp)
    token = token_for(client, "emp1")
    assert client.get(API, headers=bearer(token)).status_code == 403
    assert client.get("/api/v1/settings/payslip", headers=bearer(token)).status_code == 403
