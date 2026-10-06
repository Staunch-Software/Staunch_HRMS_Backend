"""ESI (Employee State Insurance) calculation from the active ESIConfig row.

SIMPLIFICATION (documented per the Phase 2 spec): real ESI eligibility is
all-or-nothing for an entire contribution period (Apr-Sep or Oct-Mar),
decided once from wages at the START of that period — an employee who
crosses the threshold mid-period stays contributing (or stays exempt) until
the period ends. This implementation instead re-checks eligibility on every
run from that run's own ESI_WAGES, which is materially simpler but can
diverge from the real rule when wages cross the threshold mid-period. A more
accurate implementation would need to persist eligibility state per employee
per contribution-period across runs — flagged as a future enhancement, not
built here.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.statutory import ESIConfig


def get_active_esi_config(db: Session, as_of: date) -> ESIConfig | None:
    return (
        db.query(ESIConfig)
        .filter(
            ESIConfig.is_active.is_(True),
            ESIConfig.effective_from <= as_of,
            (ESIConfig.effective_to.is_(None)) | (ESIConfig.effective_to >= as_of),
        )
        .order_by(ESIConfig.effective_from.desc())
        .first()
    )


def calculate_esi(esi_wages: Decimal, config: ESIConfig) -> dict:
    """Returns {employee_esi, employer_esi, eligible}."""
    threshold = Decimal(str(config.wage_eligibility_threshold))
    eligible = esi_wages <= threshold
    if not eligible:
        return {"employee_esi": Decimal("0"), "employer_esi": Decimal("0"), "eligible": False}

    employee_rate = Decimal(str(config.employee_rate))
    employer_rate = Decimal(str(config.employer_rate))
    return {
        "employee_esi": esi_wages * employee_rate,
        "employer_esi": esi_wages * employer_rate,
        "eligible": True,
    }
