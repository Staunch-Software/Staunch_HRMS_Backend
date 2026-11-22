"""TDS on salary, tax year 2026-27 (rules seeded by statutory_seed.seed_tds).

New regime: 0-4L nil, 4-8L 5%, 8-12L 10%, 12-16L 15%, 16-20L 20%, 20-24L 25%,
above 24L 30%; rebate up to 60,000 when taxable <= 12L, marginal relief above;
surcharge 10/15/25% above 50L/1Cr/2Cr with marginal relief; cess 4%.
Old regime: 0-2.5L nil, 2.5-5L 5%, 5-10L 20%, above 10L 30%; rebate up to
12,500 when taxable <= 5L (no marginal relief).
"""
from datetime import date
from decimal import Decimal

import pytest

from app.db.seed.pay_groups_seed import seed_pay_groups
from app.db.seed.statutory_seed import TDS_SEED, seed_tds
from app.models.employee import Employee
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.salary_component import SalaryComponent
from app.models.statutory import EmployeeTaxDeclaration
from app.models.variable_pay import VariablePayInput
from app.services.payroll_engine.engine import calculate_payroll_run
from app.services.payroll_engine.pay_groups import PayInputs, get_strategy, param_spec
from app.services.payroll_engine.pay_groups.base import xl_roundup
from app.services.payroll_engine.statutory.tds import (
    compute_annual_tax,
    compute_monthly_tds,
    financial_year_for,
    months_remaining_in_fy,
)

NEW_CONFIG, NEW_SLABS = TDS_SEED["2026-27"]["New"]
OLD_CONFIG, OLD_SLABS = TDS_SEED["2026-27"]["Old"]
NEW = {"regime": "New", **NEW_CONFIG}
OLD = {"regime": "Old", **OLD_CONFIG}


def D(value) -> Decimal:
    return Decimal(str(value))


# --- annual tax -----------------------------------------------------------------

def test_new_regime_12_lakh_is_fully_rebated():
    tax = compute_annual_tax(1200000, NEW, NEW_SLABS)
    assert tax["tax"] == D(60000)
    assert tax["rebate"] == D(60000)
    assert tax["total"] == 0


def test_new_regime_marginal_relief_just_above_12_lakh():
    # Slab tax 20,000 + 40,000 + 1,500 = 61,500 is capped at the 10,000 above 12L.
    tax = compute_annual_tax(1210000, NEW, NEW_SLABS)
    assert tax["tax"] == D(61500)
    assert tax["rebate"] == 0
    assert tax["marginal_relief"] == D(51500)
    assert tax["tax"] - tax["marginal_relief"] == D(10000)
    assert tax["cess"] == D(400)
    assert tax["total"] == D(10400)


def test_new_regime_no_marginal_relief_once_tax_is_below_the_excess():
    tax = compute_annual_tax(1325000, NEW, NEW_SLABS)
    assert tax["marginal_relief"] == 0
    assert tax["total"] == D(81900)  # 78,750 + 4% cess


def test_new_regime_30_lakh():
    # 20,000 + 40,000 + 60,000 + 80,000 + 1,00,000 + 6,00,000 x 30% = 4,80,000; cess 19,200
    tax = compute_annual_tax(3000000, NEW, NEW_SLABS)
    assert tax["tax"] == D(480000)
    assert tax["surcharge"] == 0
    assert tax["cess"] == D(19200)
    assert tax["total"] == D(499200)


def test_old_regime_8_lakh():
    # 12,500 + 3,00,000 x 20% = 72,500; cess 2,900
    tax = compute_annual_tax(800000, OLD, OLD_SLABS)
    assert tax["tax"] == D(72500)
    assert tax["rebate"] == 0
    assert tax["total"] == D(75400)


def test_old_regime_rebate_has_no_marginal_relief():
    assert compute_annual_tax(500000, OLD, OLD_SLABS)["total"] == 0
    tax = compute_annual_tax(500100, OLD, OLD_SLABS)
    assert tax["marginal_relief"] == 0
    assert tax["total"] == D(13021)  # 12,520 + 500.80 cess


def test_surcharge_at_60_lakh():
    # Tax 13,80,000; surcharge 10% 1,38,000 (cap 10,80,000 + 10,00,000 not reached); cess 60,720
    tax = compute_annual_tax(6000000, NEW, NEW_SLABS)
    assert tax["tax"] == D(1380000)
    assert tax["surcharge"] == D(138000)
    assert tax["surcharge_marginal_relief"] == 0
    assert tax["cess"] == D(60720)
    assert tax["total"] == D(1578720)


def test_surcharge_marginal_relief_just_above_50_lakh():
    # Tax 11,10,000 + surcharge 1,11,000 capped at tax on 50L (10,80,000) + 1,00,000
    tax = compute_annual_tax(5100000, NEW, NEW_SLABS)
    assert tax["surcharge"] == D(70000)
    assert tax["surcharge_marginal_relief"] == D(41000)
    assert tax["total"] == D(1227200)


def test_new_regime_surcharge_capped_at_25_percent_old_goes_to_37():
    new = compute_annual_tax(60000000, NEW, NEW_SLABS)
    old = compute_annual_tax(60000000, OLD, OLD_SLABS)
    assert new["surcharge"] == new["tax"] * D("0.25")
    assert old["surcharge"] == old["tax"] * D("0.37")


def test_financial_year_helpers():
    assert financial_year_for(date(2026, 9, 30)) == "2026-27"
    assert financial_year_for(date(2027, 3, 31)) == "2026-27"
    assert [months_remaining_in_fy(date(2026, m, 28)) for m in (4, 9, 10, 12)] == [12, 7, 6, 4]
    assert [months_remaining_in_fy(date(2027, m, 28)) for m in (1, 3)] == [3, 1]


# --- monthly projection --------------------------------------------------------------

def _employee(db, code="T-1"):
    employee = Employee(employee_code=code, first_name=code, status="Active", date_of_joining=date(2024, 1, 1))
    db.add(employee)
    db.commit()
    db.refresh(employee)
    return employee


def _schedule(db):
    schedule = PaySchedule(schedule_name="Monthly", pay_frequency="Monthly", payment_day_rule={"type": "LastDayOfMonth"},
                           holiday_adjustment="Before", is_active=True, applies_to=None)
    db.add(schedule)
    db.commit()
    return schedule


def _past_run(db, schedule, employee, period_end, status, gross, tds, tds_code="TDS"):
    run = PayrollRun(pay_schedule_id=schedule.id, period_start=period_end.replace(day=1), period_end=period_end,
                     pay_date=period_end, status=status)
    db.add(run)
    db.flush()
    db.add(PayrollRunLineItem(
        payroll_run_id=run.id, employee_id=employee.id, employee_snapshot={}, gross_earnings=gross,
        total_deductions=tds, total_reimbursements=0, net_pay=gross - tds, status="Included",
        computation_breakdown=[
            {"component_code": "BASIC", "component_type": "Earning", "resolved_amount": gross},
            {"component_code": tds_code, "component_type": "Deduction", "resolved_amount": tds},
        ],
    ))
    db.commit()
    return run


def test_october_projection_uses_approved_ytd(db_session):
    seed_tds(db_session)
    employee = _employee(db_session)
    schedule = _schedule(db_session)
    db_session.add(SalaryComponent(name="Income Tax", code="INCOME_TAX", component_type="Deduction",
                                   calculation_method="Fixed Amount", statutory_type="TDS", status=True))
    db_session.commit()
    month_ends = [date(2026, 4, 30), date(2026, 5, 31), date(2026, 6, 30),
                  date(2026, 7, 31), date(2026, 8, 31), date(2026, 9, 30)]
    for i, period_end in enumerate(month_ends):
        # Component-engine runs carry the TDS component's own code; pay-group runs carry "TDS".
        _past_run(db_session, schedule, employee, period_end, "Paid" if i < 3 else "Approved", 150000, 12000,
                  tds_code="INCOME_TAX" if i % 2 else "TDS")
    # Ignored: not approved, cancelled, previous financial year.
    _past_run(db_session, schedule, employee, date(2026, 9, 30), "Calculated", 999999, 99999)
    _past_run(db_session, schedule, employee, date(2026, 8, 31), "Cancelled", 999999, 99999)
    _past_run(db_session, schedule, employee, date(2026, 3, 31), "Paid", 999999, 99999)

    result = compute_monthly_tds(db_session, employee, date(2026, 10, 31), Decimal("150000"))
    # 6 x 1,50,000 + 1,50,000 x 6 = 18,00,000; taxable 17,25,000
    # tax 20,000 + 40,000 + 60,000 + 25,000 = 1,45,000 + cess 5,800 = 1,50,800
    # (1,50,800 - 72,000 YTD) / 6 = 13,133.33
    assert result["regime"] == "New"
    assert result["months_remaining"] == 6
    assert result["ytd_gross"] == D(900000)
    assert result["ytd_tds"] == D(72000)
    assert result["annual_projection"] == D(1800000)
    assert result["taxable_income"] == D(1725000)
    assert result["annual_tax"] == D(150800)
    assert result["amount"] == D(13133)
    assert result["warning"] is None


def test_no_declaration_means_new_regime(db_session):
    seed_tds(db_session)
    employee = _employee(db_session)
    result = compute_monthly_tds(db_session, employee, date(2026, 4, 30), Decimal("100000"))
    # 12,00,000 - 75,000 = 11,25,000 -> fully rebated under the New regime
    assert result["regime"] == "New"
    assert result["has_declaration"] is False
    assert result["annual_tax"] == 0
    assert result["amount"] == 0


def test_old_regime_declaration_with_deductions_and_previous_employer(db_session):
    seed_tds(db_session)
    employee = _employee(db_session)
    db_session.add(EmployeeTaxDeclaration(
        employee_id=employee.id, financial_year="2026-27", regime_choice="Old", declared_investments={},
        old_regime_deductions=200000, previous_employer_income=300000, previous_employer_tds=10000,
    ))
    db_session.commit()
    result = compute_monthly_tds(db_session, employee, date(2026, 7, 31), Decimal("100000"))
    # 9 months left: 9,00,000 + 3,00,000 previous = 12,00,000; taxable 12,00,000 - 50,000 - 2,00,000 = 9,50,000
    # tax 12,500 + 4,50,000 x 20% = 1,02,500 + cess 4,100 = 1,06,600; (1,06,600 - 10,000) / 9 = 10,733.33
    assert result["regime"] == "Old"
    assert result["months_remaining"] == 9
    assert result["annual_projection"] == D(1200000)
    assert result["taxable_income"] == D(950000)
    assert result["annual_tax"] == D(106600)
    assert result["amount"] == D(10733)


def test_missing_config_gives_zero_with_warning(db_session):
    employee = _employee(db_session)
    result = compute_monthly_tds(db_session, employee, date(2026, 10, 31), Decimal("500000"))
    assert result["amount"] == 0
    assert "No active New-regime TDS config for FY 2026-27" in result["warning"]


def test_component_engine_tds(db_session):
    from app.db.seed.salary_components_seed import SALARY_COMPONENTS_SEED
    from app.models.salary_structure import EmployeeSalaryStructureAssignment

    seed_tds(db_session)
    employee = _employee(db_session)
    basic = SalaryComponent(**next(c for c in SALARY_COMPONENTS_SEED if c["code"] == "BASIC"))
    tds = SalaryComponent(name="Income Tax", code="INCOME_TAX", component_type="Deduction", status=True,
                          calculation_method="Slab Based", statutory_type="TDS", rounding_rule="Upper Rupee")
    db_session.add_all([basic, tds])
    db_session.commit()
    for component, override in ((basic, 200000), (tds, None)):
        db_session.add(EmployeeSalaryStructureAssignment(employee_id=employee.id, salary_component_id=component.id,
                                                         value_override=override, effective_from=date(2025, 1, 1)))
    schedule = _schedule(db_session)
    run = PayrollRun(pay_schedule_id=schedule.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30),
                     pay_date=date(2026, 9, 30), status="Draft")
    db_session.add(run)
    db_session.commit()
    calculate_payroll_run(db_session, run)
    item = db_session.query(PayrollRunLineItem).filter(PayrollRunLineItem.payroll_run_id == run.id).one()
    entry = next(e for e in item.computation_breakdown if e["component_code"] == "INCOME_TAX")
    assert entry["resolved_amount"] == 11700
    assert float(item.net_pay) == 188300


# --- pay group runs ----------------------------------------------------------------

def _pay_group_run(client, db, group_code, monthly_salary=None, daily_wage=None, tds_enabled=True, vi=None):
    seed_pay_groups(db)
    seed_tds(db)
    group = db.query(PayGroup).filter(PayGroup.code == group_code).one()
    group.definition = {**group.definition, "tds_enabled": tds_enabled}
    employee = _employee(db, "PG-1")
    db.add(EmployeePayProfile(employee_id=employee.id, pay_group_id=group.id, monthly_salary=monthly_salary,
                              daily_wage=daily_wage, skill_level="Skilled", extra={}, effective_from=date(2026, 1, 1)))
    if vi:
        db.add(VariablePayInput(employee_id=employee.id, period_start=date(2026, 9, 1), period_end=date(2026, 9, 30), **vi))
    schedule = _schedule(db)
    run = PayrollRun(pay_schedule_id=schedule.id, pay_group_id=group.id, period_start=date(2026, 9, 1),
                     period_end=date(2026, 9, 30), pay_date=date(2026, 9, 30), status="Draft")
    db.add(run)
    db.commit()
    calculate_payroll_run(db, run)
    item = db.query(PayrollRunLineItem).filter(PayrollRunLineItem.payroll_run_id == run.id).one()
    return group, employee, item


def _tds_entries(item):
    return [e for e in item.computation_breakdown if e["component_code"] == "TDS"]


def test_pay_group_run_adds_tds_line_and_reduces_net(client, db_session):
    # CHENNAI_HO, no PF/ESI: whole 2,00,000 is stipend, net rounding NONE.
    # September: 7 months left -> projection 14,00,000, taxable 13,25,000, tax 81,900 -> 11,700 a month.
    _, _, item = _pay_group_run(client, db_session, "CHENNAI_HO", monthly_salary=200000)
    assert item.status == "Included", item.error_message
    [entry] = _tds_entries(item)
    assert entry["component_type"] == "Deduction"
    assert entry["resolved_amount"] == 11700
    assert float(item.gross_earnings) == 200000
    assert float(item.total_deductions) == 11700
    assert float(item.net_pay) == 188300


def test_pay_group_tds_keeps_net_rounding_consistent(client, db_session):
    vi = {"present_days": 24, "lop_days": 2, "overtime_hours": 0, "ad_hoc_earnings": [],
          "ad_hoc_deductions": [{"component_code": "LOAN", "amount": 500}]}
    group, employee, item = _pay_group_run(client, db_session, "CCTV", daily_wage=12000, vi=vi)
    profile = db_session.query(EmployeePayProfile).filter(EmployeePayProfile.employee_id == employee.id).one()
    plain = get_strategy("DAILY_WAGE_ATTENDANCE").calculate(
        profile, employee, PayInputs.from_variable_input(db_session.query(VariablePayInput).one()), group.params
    )
    [entry] = _tds_entries(item)
    tds = D(entry["resolved_amount"])
    assert tds > 0
    gross_raw = plain.gross_before_round_off()
    expected_net = xl_roundup(gross_raw - plain.total_deductions - tds)
    assert D(item.net_pay) == expected_net
    assert D(item.total_deductions) == plain.total_deductions + tds
    assert D(item.gross_earnings) - D(item.total_deductions) == D(item.net_pay)
    assert sum(1 for e in item.computation_breakdown if e["component_code"] == "ROUND_OFF") <= 1


def test_pay_group_tds_disabled(client, db_session):
    _, _, item = _pay_group_run(client, db_session, "CHENNAI_HO", monthly_salary=200000, tds_enabled=False)
    assert _tds_entries(item) == []
    assert float(item.net_pay) == 200000


def test_low_earner_gets_no_tds_line(client, db_session):
    _, _, item = _pay_group_run(client, db_session, "CHENNAI_HO", monthly_salary=50000)
    assert _tds_entries(item) == []
    assert float(item.net_pay) == 50000


def test_pay_group_without_tds_key_defaults_to_enabled(client, db_session):
    seed_pay_groups(db_session)
    group = db_session.query(PayGroup).filter(PayGroup.code == "CHENNAI_HO").one()
    group.definition = {k: v for k, v in group.definition.items() if k != "tds_enabled"}
    db_session.commit()
    assert get_strategy(group.calc_model).validate_params(group.definition) == []
    body = client.get(f"/api/v1/pay-groups/{group.id}").json()
    assert body["tds_enabled"] is True and body["params"]["tds_enabled"] is True
    # Legacy strategy params saved before the key existed stay valid too.
    legacy = {k: v for k, v in group.params.items() if k != "tds_enabled"}
    assert get_strategy("MONTHLY_SALARIED").validate_params(legacy) == []
    assert param_spec("MONTHLY_SALARIED")["tds_enabled"]["default"] is True


# --- API ----------------------------------------------------------------------------

def test_tax_declaration_and_preview_api(client, db_session):
    seed_tds(db_session)
    employee = _employee(db_session)
    url = f"/api/v1/employees/{employee.id}/tax-declaration/2026-27"

    default = client.get(url)
    assert default.status_code == 200, default.text
    assert default.json()["regime_choice"] == "New" and default.json()["id"] is None

    saved = client.put(url, json={"regime_choice": "Old", "old_regime_deductions": 200000,
                                  "previous_employer_income": 300000, "previous_employer_tds": 10000})
    assert saved.status_code == 200, saved.text
    assert saved.json()["id"] is not None and saved.json()["regime_choice"] == "Old"
    assert client.get(url).json()["old_regime_deductions"] == 200000

    assert client.put(url, json={"regime_choice": "Mixed"}).status_code == 422
    assert client.put(url, json={"old_regime_deductions": -1}).status_code == 422
    assert client.get(f"/api/v1/employees/{employee.id}/tax-declaration/2026-28").status_code == 400
    assert client.get("/api/v1/employees/99999/tax-declaration/2026-27").status_code == 404

    preview = client.get(f"/api/v1/employees/{employee.id}/tds-preview",
                         params={"period_end": "2026-07-31", "monthly_gross": 100000})
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["regime"] == "Old" and body["gross_source"] == "query"
    assert body["annual_projection"] == 1200000
    assert body["annual_tax"] == 106600
    assert body["monthly_tds"] == 10733
    assert body["tax_breakdown"]["cess"] == 4100

    none = client.get(f"/api/v1/employees/{employee.id}/tds-preview", params={"period_end": "2026-07-31"}).json()
    assert none["gross_source"] == "none" and none["current_month_gross"] == 0


@pytest.mark.parametrize("regime", ["New", "Old"])
def test_seed_tds_is_verified_and_idempotent(db_session, regime):
    from app.models.statutory import TDSConfig, TDSSlab

    seed_tds(db_session)
    seed_tds(db_session)
    configs = db_session.query(TDSConfig).filter(TDSConfig.regime == regime).all()
    slabs = db_session.query(TDSSlab).filter(TDSSlab.regime == regime).all()
    assert len(configs) == 1 and configs[0].is_verified and configs[0].financial_year == "2026-27"
    assert len(slabs) == len(TDS_SEED["2026-27"][regime][1]) and all(s.is_verified for s in slabs)
