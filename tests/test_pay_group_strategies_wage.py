"""Pay-group strategy tests for the day-wage models (KKNPP_HARI, MCBS, CCTV,
ESFAS, BHAVNAGAR). Synthetic data; expected values hand-calculated below.
"""
import copy
from decimal import Decimal

import pytest

from app.db.seed.pay_groups_seed import SEED_PARAMS_BY_CODE
from app.services.payroll_engine.pay_groups import (
    PayGroupCalculationError,
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


# --- KKNPP_HARI / DAILY_WAGE_STANDARD_DAYS ----------------------------------------------

def hari(profile, inputs):
    return calc("KKNPP_HARI", "DAILY_WAGE_STANDARD_DAYS", profile, inputs)


def test_hari_prorated_ot_unrounded_bonus_capped_pf_capped():
    # G=1000, W=26, Y=24, OT 10 h, wage arrear 200
    # H = 26000; K = 13000; L = 6500; M = 6500; O = 149.73*26 = 3892.98
    # AA = 13000*24/26 = 12000; AB = 6000; AC = 6000; AD = ROUND(3593.52) = 3594 -> AH = 27594
    # Z = ROUNDUP(26000*24/26) = 24000; bonus = MIN(1999.2, 21000*0.0833 = 1749.3) = 1749.3
    # OT = (1000*2/9)*10 = 2222.2222 (not rounded)
    # PF = ROUNDUP(MIN(27594*0.12 = 3311.28, 1800)) = 1800
    # net = 27594 - 1800 + 2222.2222 + 1749.3 + 200 = 29965.52
    result = hari(PayProfileData(daily_wage=1000, pf_applicable=True),
                  PayInputs(present_days=D(24), overtime_hours=D(10), earnings={"WAGE_ARREAR": D(200)}))
    assert line(result, "SPL_ALLOWANCE") == 3594
    assert line(result, "BONUS") == D("1749.3")
    assert line(result, "PF_EE") == 1800
    assert result.net_pay == D("29965.52")
    # Employer PF (CTC only) is always 15000*12% — the effective behaviour of the sheet's IF.
    assert line(result, "PF_ER") == 1800
    assert_balanced(result)


def test_hari_bonus_and_pf_below_caps():
    # G=500, Y=13: H = 13000; K = 6500; L = 3250; M = 3250
    # AA = 3250; AB = 1625; AC = 1625; AD = ROUND(3892.98*13/26 = 1946.49) = 1946 -> AH = 8446
    # bonus = MIN(6500*0.0833 = 541.45, 1749.3) = 541.45
    # PF = ROUNDUP(8446*0.12 = 1013.52) = 1014; net = 8446 - 1014 + 541.45 = 7973.45
    result = hari(PayProfileData(daily_wage=500, pf_applicable=True), PayInputs(present_days=D(13)))
    assert line(result, "BONUS") == D("541.45")
    assert line(result, "PF_EE") == 1014
    assert result.net_pay == D("7973.45")


def test_hari_no_pf():
    # As above without PF: 8446 + 541.45 = 8987.45
    result = hari(PayProfileData(daily_wage=500, pf_applicable=False), PayInputs(present_days=D(13)))
    assert line(result, "PF_EE") == 0
    assert result.net_pay == D("8987.45")


# --- MCBS / CCTV / ESFAS (DAILY_WAGE_ATTENDANCE) ------------------------------------------

def attendance(code, profile, inputs, params=None):
    return calc(code, "DAILY_WAGE_ATTENDANCE", profile, inputs, params)


def test_mcbs_pf_below_threshold_ot_loan_excess_wage():
    # D=500, 20 days: amount 10000; allowance 149.73*20 = 2994.6
    # OT = (500/9*2)*4 = 444.444; bonus = MIN(833, 1749.3) = 833
    # total = 14272.0444; PF: 10000 <= 15000 -> 10000*0.12 = 1200
    # net = ROUNDUP(14272.0444 - 1200 - 300 - 100) = ROUNDUP(12672.04) = 12673
    result = attendance("MCBS", PayProfileData(daily_wage=500, skill_level="Highly Skilled"),
                        PayInputs(present_days=D(20), overtime_hours=D(4),
                                  deductions={"LOAN": D(300), "EXCESS_WAGE_DEDUCTION": D(100)}))
    assert line(result, "PF_EE") == 1200
    assert line(result, "BONUS") == 833
    assert result.net_pay == 12673
    assert line(result, "ROUND_OFF") > 0
    assert_balanced(result)


def test_mcbs_pf_flat_above_threshold_and_bonus_cap():
    # D=1000, 26 days: amount 26000 > 15000 -> PF = 25000*0.12 = 3000
    # bonus = MIN(2165.8, 1749.3) = 1749.3; allowance = 149.73*26 = 3892.98 (DEFAULT rate)
    # net = ROUNDUP(26000 + 3892.98 + 1749.3 - 3000) = ROUNDUP(28642.28) = 28643
    result = attendance("MCBS", PayProfileData(daily_wage=1000, skill_level="Skilled"), PayInputs(present_days=D(26)))
    assert line(result, "PF_EE") == 3000
    assert line(result, "BONUS") == D("1749.3")
    assert result.net_pay == 28643


def test_cctv_skill_rates_transport_outside_pf_below_threshold():
    # Skilled D=1000, 24 days: amount 24000 (<= 25000 -> PF 24000*0.12 = 2880)
    # allowance 144.73*24 = 3473.52; transport 34.73*24 = 833.52; bonus 1749.3
    # net = ROUNDUP(24000 + 3473.52 + 1749.3 + 833.52 - 2880) = ROUNDUP(27176.34) = 27177
    result = attendance("CCTV", PayProfileData(daily_wage=1000, skill_level="Skilled"), PayInputs(present_days=D(24)))
    assert line(result, "ALLOWANCE") == D("3473.52")
    assert line(result, "TRANSPORT") == D("833.52")
    assert line(result, "PF_EE") == 2880
    assert result.net_pay == 27177


def test_cctv_pf_flat_above_25000():
    # Highly-Skilled D=1000, 26 days: 26000 > 25000 -> PF 3000
    # allowance 149.73*26 = 3892.98; transport 35.93*26 = 934.18; bonus 1749.3
    # net = ROUNDUP(26000 + 3892.98 + 1749.3 + 934.18 - 3000) = ROUNDUP(29576.46) = 29577
    result = attendance("CCTV", PayProfileData(daily_wage=1000, skill_level="Highly-Skilled"),
                        PayInputs(present_days=D(26)))
    assert line(result, "PF_EE") == 3000
    assert result.net_pay == 29577


def test_cctv_loan_is_deducted_and_special_allowance_paid():
    # Skilled 24-day case + special allowance 500 - loan 400: ROUNDUP(27176.34 + 500 - 400) = 27277
    result = attendance("CCTV", PayProfileData(daily_wage=1000, skill_level="Skilled"),
                        PayInputs(present_days=D(24), earnings={"SPECIAL_ALLOWANCE": D(500)},
                                  deductions={"LOAN": D(400)}))
    assert line(result, "LOAN") == 400
    assert result.net_pay == 27277


def test_cctv_unknown_skill_raises():
    with pytest.raises(PayGroupCalculationError):
        attendance("CCTV", PayProfileData(daily_wage=1000, skill_level="Unskilled"), PayInputs(present_days=D(24)))


def test_esfas_fixed_monthly_amount_with_ot_on_8_hour_day():
    # fixed 40000; no allowance/bonus/transport; OT = (1000/8*2)*2 = 500
    # PF wage = D*days = 22000 <= 25000 -> 2640; paid leave 1000
    # net = ROUNDUP(40000 + 500 + 1000 - 2640) = 38860
    result = attendance("ESFAS", PayProfileData(daily_wage=1000, skill_level="Skilled", fixed_monthly_amount=40000),
                        PayInputs(present_days=D(22), overtime_hours=D(2), earnings={"PAID_LEAVE": D(1000)}))
    assert line(result, "FIXED_AMOUNT") == 40000
    assert line(result, "ALLOWANCE") == 0 and line(result, "BONUS") == 0
    assert line(result, "OT") == 500
    assert line(result, "PF_EE") == 2640
    assert result.net_pay == 38860


def test_missing_input_row_defaults_to_standard_days_with_note():
    result = attendance("MCBS", PayProfileData(daily_wage=1000), PayInputs(has_input_row=False))
    assert result.paid_days == 26
    assert any("No variable input" in w for w in result.warnings)


# --- BHAVNAGAR / DAILY_WAGE_BASIC_DA ----------------------------------------------------

def bhavnagar(profile, inputs, params=None):
    return calc("BHAVNAGAR", "DAILY_WAGE_BASIC_DA", profile, inputs, params)


def test_bhavnagar_lop_esi_pt_adjustment_half_up():
    # Skilled G=1000, LOP 3 -> K = 23; H = 26000; L = ROUNDUP(26000*23/26) = 23000
    # M = 579*23 = 13317; N = 339*23 = 7797; O = 23000 - 21114 = 1886; P = 23000
    # PF = ROUND(21114*0.12 = 2533.68) = 2534; ESI = 23000*0.0075 = 172.5 (not rounded); PT 208
    # net = ROUND(23000 - 2534 - 172.5 - 208 + 99) = ROUND(20184.5) = 20185 (banker's would give 20184)
    result = bhavnagar(PayProfileData(daily_wage=1000, skill_level="Skilled", esi_applicable=True, pf_applicable=True),
                       PayInputs(lop_days=D(3), earnings={"ADJUSTMENT": D(99)}))
    assert line(result, "SPL_ALLOWANCE") == 1886
    assert line(result, "PF_EE") == 2534
    assert line(result, "ESI_EE") == D("172.5")
    assert line(result, "PT") == 208
    assert result.net_pay == 20185
    assert line(result, "ESI_ER") == D("747.5")
    assert line(result, "INSURANCE_ER") == 350
    assert_balanced(result)


def test_bhavnagar_pf_wage_ceiling_and_no_esi():
    # Params are data: raise per-day Basic/DA to 700/400 -> M+N = 1100*26 = 28600 > 25000
    # PF = ROUND(25000*0.12) = 3000; G=1200 -> L = 31200; no ESI
    # net = ROUND(31200 - 3000 - 208) = 27992
    params = copy.deepcopy(SEED_PARAMS_BY_CODE["BHAVNAGAR"])
    params["basic_per_day_by_skill"]["SKILLED"] = 700
    params["da_per_day_by_skill"]["SKILLED"] = 400
    result = bhavnagar(PayProfileData(daily_wage=1200, skill_level="Skilled", esi_applicable=False),
                       PayInputs(present_days=D(26)), params)
    assert line(result, "PF_EE") == 3000
    assert line(result, "ESI_EE") == 0
    assert result.net_pay == 27992
