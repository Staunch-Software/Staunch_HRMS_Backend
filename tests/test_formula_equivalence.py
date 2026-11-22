"""The seven location pay groups as FORMULA definitions (converted from each
strategy's params) give exactly the strategy's result: same line items
(code, label, kind, amount, order), gross, deductions, net, employer cost,
paid/LOP days, PF/ESI wages and warnings — for every case of the strategy
tests plus a seeded random grid, and after the TDS step.
"""
import copy
import random
from decimal import Decimal

import pytest

from app.db.seed.pay_groups_seed import PAY_GROUPS_SEED, SEED_CALC_MODEL_BY_CODE, SEED_PARAMS_BY_CODE
from app.services.payroll_engine.formula.history import converted_definition
from app.services.payroll_engine.pay_groups import (
    PayGroupCalculationError,
    PayInputs,
    PayProfileData,
    get_strategy,
)

D = Decimal
P, I = PayProfileData, PayInputs


def run(engine_model, profile, inputs, config, employee=None, tds=None):
    try:
        result = get_strategy(engine_model).calculate(profile, employee, copy.deepcopy(inputs), config)
    except PayGroupCalculationError:
        return ("error",)
    if tds:
        result.add_deduction("TDS", "Income tax (TDS)", D(tds), "test", config["net_rounding"])
    return (
        [(ln.code, ln.label, ln.kind, ln.amount) for ln in result.lines],
        result.gross, result.total_deductions, result.net_pay, result.employer_cost,
        result.paid_days, result.lop_days, result.pf_wages, result.esi_wages, result.warnings,
    )


def assert_same(code, profile, inputs, params=None, employee=None, tds=None):
    params = params if params is not None else SEED_PARAMS_BY_CODE[code]
    model = SEED_CALC_MODEL_BY_CODE[code]
    definition = converted_definition(model, params, code)
    expected = run(model, profile, inputs, params, employee, tds)
    actual = run("FORMULA", profile, inputs, definition, employee, tds)
    assert actual == expected, f"{code}: {profile} {inputs}"
    return expected


# Every case of tests/test_pay_group_strategies*.py.
STRATEGY_TEST_CASES = [
    ("CHENNAI_HO", P(monthly_salary=50000, pf_applicable=True, esi_applicable=False),
     I(lop_days=D(3), overtime_days=D(2), earnings={"OTHER_PAYMENT": D(500)}, deductions={"OTHER_ADVANCE": D(1000)})),
    ("CHENNAI_HO", P(monthly_salary=50000, pf_applicable=True, esi_applicable=False, pf_cap=1800), I()),
    ("CHENNAI_HO", P(monthly_salary=19000, pf_applicable=True, esi_applicable=True), I(lop_days=D(1))),
    ("CHENNAI_HO", P(monthly_salary=15000, pf_applicable=False, esi_applicable=False),
     I(lop_days=D(2), overtime_days=D(1))),
    ("CHENNAI_HO", P(monthly_salary=50000), I()),
    ("TUTICORIN", P(daily_wage=500, extra={"supervisor_allowance": 1000}), I(present_days=D(26))),
    ("TUTICORIN", P(daily_wage=500, extra={"supervisor_allowance": 1000}),
     I(present_days=D("25.5"), earnings={"SUPERVISOR_ALLOWANCE": D(1500)})),
    ("TUTICORIN", P(extra={"base_day_rate": 500}), I(present_days=D(0))),
    ("KKNPP_HARI", P(daily_wage=1000, pf_applicable=True),
     I(present_days=D(24), overtime_hours=D(10), earnings={"WAGE_ARREAR": D(200)})),
    ("KKNPP_HARI", P(daily_wage=500, pf_applicable=True), I(present_days=D(13))),
    ("KKNPP_HARI", P(daily_wage=500, pf_applicable=False), I(present_days=D(13))),
    ("MCBS", P(daily_wage=500, skill_level="Highly Skilled"),
     I(present_days=D(20), overtime_hours=D(4), deductions={"LOAN": D(300), "EXCESS_WAGE_DEDUCTION": D(100)})),
    ("MCBS", P(daily_wage=1000, skill_level="Skilled"), I(present_days=D(26))),
    ("CCTV", P(daily_wage=1000, skill_level="Skilled"), I(present_days=D(24))),
    ("CCTV", P(daily_wage=1000, skill_level="Highly-Skilled"), I(present_days=D(26))),
    ("CCTV", P(daily_wage=1000, skill_level="Skilled"),
     I(present_days=D(24), earnings={"SPECIAL_ALLOWANCE": D(500)}, deductions={"LOAN": D(400)})),
    ("CCTV", P(daily_wage=1000, skill_level="Unskilled"), I(present_days=D(24))),
    ("ESFAS", P(daily_wage=1000, skill_level="Skilled", fixed_monthly_amount=40000),
     I(present_days=D(22), overtime_hours=D(2), earnings={"PAID_LEAVE": D(1000)})),
    ("MCBS", P(daily_wage=1000), I(has_input_row=False)),
    ("BHAVNAGAR", P(daily_wage=1000, skill_level="Skilled", esi_applicable=True, pf_applicable=True),
     I(lop_days=D(3), earnings={"ADJUSTMENT": D(99)})),
    ("ESFAS", P(fixed_monthly_amount=40000), I()),
    ("ESFAS", P(), I()),
    ("BHAVNAGAR", P(skill_level="Expert"), I()),
]


@pytest.mark.parametrize("code,profile,inputs", STRATEGY_TEST_CASES)
def test_strategy_test_cases_match(code, profile, inputs):
    assert_same(code, profile, inputs)
    assert_same(code, profile, inputs, tds=1234)


def test_hr_edited_params_carry_over():
    params = copy.deepcopy(SEED_PARAMS_BY_CODE["BHAVNAGAR"])
    params["basic_per_day_by_skill"]["SKILLED"] = 700
    params["da_per_day_by_skill"]["SKILLED"] = 400
    result = assert_same("BHAVNAGAR", P(daily_wage=1200, skill_level="Skilled", esi_applicable=False),
                         I(present_days=D(26)), params)
    assert result[3] == 27992
    cctv = copy.deepcopy(SEED_PARAMS_BY_CODE["CCTV"])
    cctv["allowance_per_day_by_skill"]["SKILLED"] = 150
    cctv["pf_threshold"] = 10000
    assert_same("CCTV", P(daily_wage=700, skill_level="Skilled"), I(present_days=D(20)), cctv)


def test_flags_fall_back_to_employee_then_group_default():
    class Emp:
        pf_applicable = True
        esi_applicable = None

    for code in SEED_PARAMS_BY_CODE:
        profile = P(monthly_salary=50000, daily_wage=900, skill_level="Skilled")
        assert_same(code, profile, I(present_days=D(20)), employee=Emp())


SKILLS = ["Skilled", "Semi-Skilled", "Highly Skilled", "HIGHLY_SKILLED", None, "Unskilled"]
DAYS = [D(0), D(1), D("12.5"), D(20), D(25), D(26), D(30), None]


def _random_case(rng: random.Random, code: str):
    flag = lambda: rng.choice([True, False, None])  # noqa: E731
    money = lambda hi: D(rng.randint(0, hi * 100)) / 100  # noqa: E731
    profile = P(
        monthly_salary=rng.choice([None, money(250000), 15000, 21000, 50000]),
        daily_wage=rng.choice([None, money(3000), 500, 1000]),
        skill_level=rng.choice(SKILLS), pf_applicable=flag(), esi_applicable=flag(),
        pf_cap=rng.choice([None, 1800, 3000, 0]),
        fixed_monthly_amount=rng.choice([None, None, None, money(60000)]),
        extra=rng.choice([{}, {"supervisor_allowance": 1000}, {"base_day_rate": 582},
                          {"base_day_rate": 600, "supervisor_allowance": "2000"}]),
    )
    earnings = {c: money(3000) for c in ("OTHER_PAYMENT", "SUPERVISOR_ALLOWANCE", "SPECIAL_ALLOWANCE", "PAID_LEAVE",
                                         "WAGE_ARREAR", "ADJUSTMENT", "BONUS_X") if rng.random() < 0.2}
    deductions = {c: money(2000) for c in ("OTHER_ADVANCE", "LOAN", "EXCESS_WAGE_DEDUCTION") if rng.random() < 0.2}
    if rng.random() < 0.1:
        earnings["SUPERVISOR_ALLOWANCE"] = D(0)
    inputs = I(
        present_days=rng.choice(DAYS), lop_days=rng.choice([D(0), D(0), D("1.5"), D(3), D(10)]),
        overtime_hours=rng.choice([D(0), D(0), D(4), D("7.5")]), overtime_days=rng.choice([D(0), D(2), D("0.5")]),
        earnings=earnings, deductions=deductions, has_input_row=rng.random() > 0.1,
    )
    return profile, inputs


@pytest.mark.parametrize("code", [g["code"] for g in PAY_GROUPS_SEED])
def test_random_grid_matches(code):
    rng = random.Random(f"formula-equivalence-{code}")
    outcomes = {"ok": 0, "error": 0}
    for _ in range(400):
        profile, inputs = _random_case(rng, code)
        expected = assert_same(code, profile, inputs, tds=rng.choice([None, 0, 517]))
        outcomes["error" if expected[0] == "error" else "ok"] += 1
    assert outcomes["ok"] > 100
