from datetime import date
from decimal import Decimal

from app.models.statutory import ESIConfig
from app.services.payroll_engine.statutory.esi import calculate_esi, get_active_esi_config


def _make_esi_config(**overrides):
    defaults = dict(
        effective_from=date(2024, 4, 1),
        effective_to=None,
        employee_rate=0.0075,
        employer_rate=0.0325,
        wage_eligibility_threshold=21000,
        is_active=True,
    )
    defaults.update(overrides)
    return ESIConfig(**defaults)


def test_get_active_esi_config(db_session):
    config = _make_esi_config()
    db_session.add(config)
    db_session.commit()

    found = get_active_esi_config(db_session, date(2026, 9, 30))
    assert found is not None
    assert found.id == config.id


def test_esi_not_eligible_above_threshold():
    config = _make_esi_config()
    result = calculate_esi(Decimal("84000"), config)
    assert result["eligible"] is False
    assert result["employee_esi"] == Decimal("0")


def test_esi_eligible_at_or_below_threshold():
    config = _make_esi_config()
    result = calculate_esi(Decimal("21000"), config)
    assert result["eligible"] is True
    assert result["employee_esi"] == Decimal("21000") * Decimal("0.0075")
    assert result["employee_esi"] == Decimal("157.5000")
