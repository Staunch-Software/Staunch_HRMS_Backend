"""Unit tests for the pay-group strategies, using the seeded params and
synthetic employees. Expected figures are hand-calculated in the comments
(Excel semantics: ROUND = half away from zero, ROUNDUP = away from zero).
"""
import copy
from decimal import Decimal

import pytest

from app.db.seed.pay_groups_seed import SEED_PARAMS_BY_CODE
from app.services.payroll_engine.pay_groups import (
    PayGroupCalculationError,
    PayGroupParamsError,
    PayInputs,
    PayProfileData,
    get_strategy,
)

D = Decimal


def calc(group_code, calc_model, profile, inputs, params=None):
    params = params if params is not None else SEED_PARAMS_BY_CODE[group_code]
    return get_strategy(calc_model).calculate(profile, None, inputs, params)


def line(result, code):
    return result.line_amount(code)


def assert_balanced(result):
    assert result.gross - result.total_deductions == result.net_pay


# --- CHENNAI_HO / MONTHLY_SALARIED ------------------------------------------------

def chennai(profile, inputs):
    return calc("CHENNAI_HO", "MONTHLY_SALARIED", profile, inputs)


def test_chennai_lop_ot_pf_capped_at_default_3000():
    # F=50000, PF yes, ESI no; LOP 3, OT 2 days, other payment 500, advance 1000.
    # I = ROUND(50000*0.8) = 40000; N = 28000; O = 12000; P = 50000-40000 = 10000
    # AB = 30-3 = 27; AD = 28000*27/30 = 25200; AE = 10800; AF = 9000
    # AG = ROUND(40000*2/30 = 2666.67) = 2667
    # gross = 25200+10800+9000+2667+500 = 48167
    # PF = ROUNDUP(MIN(25200*0.12 = 3024, 3000)) = 3000
    # net = 48167 - 3000 - 1000 = 44167
    # employer PF = ROUNDUP(MIN(28000*0.12 = 3360, 3000) + 28000*0.01 = 280) = 3280
    result = chennai(
        PayProfileData(monthly_salary=50000, pf_applicable=True, esi_applicable=False),
        PayInputs(lop_days=D(3), overtime_days=D(2), earnings={"OTHER_PAYMENT": D(500)},
                  deductions={"OTHER_ADVANCE": D(1000)}),
    )
    assert line(result, "BASIC_DA") == 25200
    assert line(result, "OT") == 2667
    assert result.gross == 48167
    assert line(result, "PF_EE") == 3000
    assert line(result, "ESI_EE") == 0
    assert result.net_pay == 44167
    assert line(result, "PF_ER") == 3280
    assert result.paid_days == 27 and result.lop_days == 3
    assert_balanced(result)


def test_chennai_per_employee_pf_cap_1800():
    # Same salary, full month: AD = 28000; PF = MIN(3360, 1800) = 1800; net = 50000-1800
    result = chennai(PayProfileData(monthly_salary=50000, pf_applicable=True, esi_applicable=False, pf_cap=1800),
                     PayInputs())
    assert line(result, "PF_EE") == 1800
    assert result.net_pay == 48200


def test_chennai_esi_and_pf_roundup():
    # F=19000, PF+ESI, LOP 1. I = 15200; N = 10640; O = 4560; P = 3800
    # AD = ROUND(10640*29/30 = 10285.33) = 10285; AE = ROUND(4560*29/30 = 4408) = 4408
    # AF = ROUND(3800*29/30 = 3673.33) = 3673; gross = 18366
    # PF = ROUNDUP(10285*0.12 = 1234.2) = 1235
    # ESI = ROUNDUP((10285+4408+0)*0.0075 = 110.1975) = 111
    # net = 18366 - 1235 - 111 = 17020; employer ESI = ROUNDUP(15200*0.0325 = 494) = 494
    result = chennai(PayProfileData(monthly_salary=19000, pf_applicable=True, esi_applicable=True),
                     PayInputs(lop_days=D(1)))
    assert result.gross == 18366
    assert line(result, "PF_EE") == 1235
    assert line(result, "ESI_EE") == 111
    assert result.net_pay == 17020
    assert line(result, "ESI_ER") == 494


def test_chennai_no_pf_no_esi_whole_salary_is_stipend():
    # F=15000, no PF/ESI -> I = 0 -> M (stipend) = 15000, Basic/HRA/special = 0
    # LOP 2: stipend = 15000*28/30 = 14000; OT 1 day on M: ROUND(15000/30) = 500
    # net = 14500, no statutory deductions
    result = chennai(PayProfileData(monthly_salary=15000, pf_applicable=False, esi_applicable=False),
                     PayInputs(lop_days=D(2), overtime_days=D(1)))
    assert line(result, "STIPEND") == 14000
    assert line(result, "BASIC_DA") == 0
    assert line(result, "OT") == 500
    assert result.total_deductions == 0
    assert result.net_pay == 14500


def test_chennai_flags_fall_back_to_employee_then_group_default():
    class Emp:
        pf_applicable = True
        esi_applicable = None

    strategy = get_strategy("MONTHLY_SALARIED")
    # Profile flags unset -> employee says PF yes; ESI unset everywhere -> group default (False).
    result = strategy.calculate(PayProfileData(monthly_salary=50000), Emp(), PayInputs(),
                                SEED_PARAMS_BY_CODE["CHENNAI_HO"])
    assert line(result, "PF_EE") == 3000
    assert line(result, "ESI_EE") == 0


# --- TUTICORIN / DAILY_FLAT_RATE ----------------------------------------------------

def tuticorin(profile, inputs):
    return calc("TUTICORIN", "DAILY_FLAT_RATE", profile, inputs)


def test_tuticorin_flat_pf_and_standing_supervisor_allowance():
    # per-day = 500 + 492.3 = 992.3; 26 days -> ROUND(25799.8) = 25800
    # PF = ROUND(25000*0.12) = 3000 (flat); supervisor (standing) 1000
    # net = ROUND(25800 - 3000 + 1000) = 23800
    result = tuticorin(PayProfileData(daily_wage=500, extra={"supervisor_allowance": 1000}),
                       PayInputs(present_days=D(26)))
    assert line(result, "GROSS_SALARY") == 25800
    assert line(result, "PF_EE") == 3000
    assert result.net_pay == 23800


def test_tuticorin_monthly_supervisor_input_replaces_standing_value():
    # 25.5 days x 992.3 = 25303.65 -> 25304; input supervisor 1500 replaces standing 1000
    # net = 25304 - 3000 + 1500 = 23804
    result = tuticorin(PayProfileData(daily_wage=500, extra={"supervisor_allowance": 1000}),
                       PayInputs(present_days=D("25.5"), earnings={"SUPERVISOR_ALLOWANCE": D(1500)}))
    assert line(result, "GROSS_SALARY") == 25304
    assert line(result, "SUPERVISOR_ALLOWANCE") == 1500
    assert result.net_pay == 23804


def test_tuticorin_zero_days_no_flat_pf():
    result = tuticorin(PayProfileData(extra={"base_day_rate": 500}), PayInputs(present_days=D(0)))
    assert line(result, "PF_EE") == 0
    assert result.net_pay == 0
