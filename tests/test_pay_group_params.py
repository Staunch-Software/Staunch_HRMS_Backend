import copy

import pytest

from app.db.seed.pay_groups_seed import PAY_GROUPS_SEED, SEED_PARAMS_BY_CODE
from app.services.payroll_engine.pay_groups import (
    PayGroupParamsError,
    PayInputs,
    PayProfileData,
    get_strategy,
)


def test_seeded_params_are_valid():
    assert {g["code"] for g in PAY_GROUPS_SEED} == {
        "CHENNAI_HO", "TUTICORIN", "KKNPP_HARI", "MCBS", "CCTV", "ESFAS", "BHAVNAGAR",
    }
    for group in PAY_GROUPS_SEED:
        assert get_strategy(group["calc_model"]).validate_params(group["params"]) == [], group["code"]


def test_params_validation_reports_missing_unknown_and_bad_values():
    params = copy.deepcopy(SEED_PARAMS_BY_CODE["CCTV"])
    del params["pf_threshold"]
    params["pf_treshold"] = 25000
    params["pf_rate"] = "twelve"
    params["net_rounding"] = "CEIL"
    params["allowance_per_day_by_skill"] = {"Highly Skilled": 149.73}
    text = " | ".join(get_strategy("DAILY_WAGE_ATTENDANCE").validate_params(params))
    assert "missing keys: pf_threshold" in text
    assert "unknown keys: pf_treshold" in text
    assert "'pf_rate' must be a non-negative number" in text
    assert "'net_rounding' must be one of" in text
    assert "HIGHLY_SKILLED" in text
    with pytest.raises(PayGroupParamsError):
        get_strategy("DAILY_WAGE_ATTENDANCE").calculate(
            PayProfileData(daily_wage=1000, skill_level="Skilled"), None, PayInputs(), params
        )


def test_unknown_calc_model():
    with pytest.raises(PayGroupParamsError):
        get_strategy("NOPE")


def test_profile_validation():
    strategy = get_strategy("DAILY_WAGE_BASIC_DA")
    params = SEED_PARAMS_BY_CODE["BHAVNAGAR"]
    assert strategy.validate_profile(PayProfileData(daily_wage=900, skill_level="Semi-Skilled"), params) == []
    errors = strategy.validate_profile(PayProfileData(skill_level="Expert"), params)
    assert any("daily_wage is required" in e for e in errors)
    assert any("skill_level 'Expert'" in e for e in errors)
    # A fixed-amount ESFAS engineer needs no skill rate and no day wage.
    esfas = get_strategy("DAILY_WAGE_ATTENDANCE")
    assert esfas.validate_profile(PayProfileData(fixed_monthly_amount=40000), SEED_PARAMS_BY_CODE["ESFAS"]) == []
    assert esfas.validate_profile(PayProfileData(), SEED_PARAMS_BY_CODE["ESFAS"]) != []
