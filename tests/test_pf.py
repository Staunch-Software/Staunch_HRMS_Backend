from datetime import date
from decimal import Decimal

from app.models.statutory import PFConfig
from app.services.payroll_engine.statutory.pf import calculate_pf, get_active_pf_config


def _make_pf_config(**overrides):
    defaults = dict(
        effective_from=date(2024, 4, 1),
        effective_to=None,
        employee_rate=0.12,
        employer_eps_rate=0.0833,
        employer_epf_rate=0.0367,
        wage_ceiling=15000,
        eps_wage_ceiling=15000,
        apply_ceiling=True,
        is_active=True,
    )
    defaults.update(overrides)
    return PFConfig(**defaults)


def test_get_active_pf_config_matches_effective_window(db_session):
    config = _make_pf_config()
    db_session.add(config)
    db_session.commit()

    found = get_active_pf_config(db_session, date(2026, 9, 30))
    assert found is not None
    assert found.id == config.id


def test_get_active_pf_config_ignores_expired_row(db_session):
    expired = _make_pf_config(effective_from=date(2020, 1, 1), effective_to=date(2021, 1, 1))
    db_session.add(expired)
    db_session.commit()

    found = get_active_pf_config(db_session, date(2026, 9, 30))
    assert found is None


def test_pf_capped_at_wage_ceiling_when_apply_ceiling_true():
    config = _make_pf_config()
    # PF_WAGES well above the 15,000 ceiling -> capped before applying the rate.
    result = calculate_pf(Decimal("60000"), config)
    assert result["pf_wages_for_calc"] == Decimal("15000")
    assert result["employee_pf"] == Decimal("15000") * Decimal("0.12")
    assert result["employee_pf"] == Decimal("1800.00")


def test_pf_uncapped_when_apply_ceiling_false():
    config = _make_pf_config(apply_ceiling=False)
    result = calculate_pf(Decimal("60000"), config)
    assert result["pf_wages_for_calc"] == Decimal("60000")
    assert result["employee_pf"] == Decimal("60000") * Decimal("0.12")


def test_pf_below_ceiling_uses_actual_wages():
    config = _make_pf_config()
    result = calculate_pf(Decimal("10000"), config)
    assert result["pf_wages_for_calc"] == Decimal("10000")
    assert result["employee_pf"] == Decimal("10000") * Decimal("0.12")
