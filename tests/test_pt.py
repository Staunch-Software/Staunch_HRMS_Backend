from datetime import date
from decimal import Decimal

from app.models.statutory import PTSlab, PTState
from app.services.payroll_engine.statutory.pt import calculate_pt


def _seed_tn(db_session):
    state = PTState(name="Tamil Nadu", code="TN")
    db_session.add(state)
    db_session.commit()
    db_session.refresh(state)

    slabs = [
        (0, 21000, 0),
        (21001, 30000, 135),
        (30001, 45000, 315),
        (45001, 60000, 690),
        (60001, 75000, 1025),
        (75001, None, 1250),
    ]
    for income_min, income_max, tax_amount in slabs:
        db_session.add(
            PTSlab(
                state_id=state.id,
                effective_from=date(2024, 4, 1),
                effective_to=None,
                income_min=income_min,
                income_max=income_max,
                tax_amount=tax_amount,
                periodicity="HalfYearly",
                is_verified=False,
                notes="test seed",
            )
        )
    db_session.commit()
    return state


def test_pt_matches_correct_slab(db_session):
    _seed_tn(db_session)
    result = calculate_pt(db_session, "TN", Decimal("84000"), date(2026, 9, 30))
    assert result["amount"] == Decimal("1250")
    assert result["is_verified"] is False


def test_pt_zero_slab_for_low_income(db_session):
    _seed_tn(db_session)
    result = calculate_pt(db_session, "TN", Decimal("14000"), date(2026, 9, 30))
    assert result["amount"] == Decimal("0")


def test_pt_unknown_state_returns_zero_with_warning(db_session):
    result = calculate_pt(db_session, "XX", Decimal("50000"), date(2026, 9, 30))
    assert result["amount"] == Decimal("0")
    assert "No Professional Tax slab configured" in result["warning"]


def test_pt_missing_state_code_returns_zero_with_warning(db_session):
    result = calculate_pt(db_session, None, Decimal("50000"), date(2026, 9, 30))
    assert result["amount"] == Decimal("0")
    assert result["warning"] is not None
