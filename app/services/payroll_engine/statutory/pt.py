"""Professional Tax slab lookup by work-location state code. All seeded
PT slabs are `is_verified=False` — see db/seed/pt_tds_seed.py and the phase
report. If no state config exists for the employee's state_code, PT is set
to 0 and a warning is added to the line item's computation_breakdown rather
than crashing the run.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.statutory import PTSlab, PTState


def find_pt_slab(db: Session, state_code: str, gross_earnings: Decimal, as_of: date) -> PTSlab | None:
    state = db.query(PTState).filter(PTState.code == state_code).first()
    if not state:
        return None

    slabs = (
        db.query(PTSlab)
        .filter(
            PTSlab.state_id == state.id,
            PTSlab.effective_from <= as_of,
            (PTSlab.effective_to.is_(None)) | (PTSlab.effective_to >= as_of),
        )
        .all()
    )
    for slab in slabs:
        income_min = Decimal(str(slab.income_min))
        income_max = Decimal(str(slab.income_max)) if slab.income_max is not None else None
        if gross_earnings >= income_min and (income_max is None or gross_earnings <= income_max):
            return slab
    return None


def calculate_pt(db: Session, state_code: str | None, gross_earnings: Decimal, as_of: date) -> dict:
    """Returns {amount, warning, is_verified}."""
    if not state_code:
        return {
            "amount": Decimal("0"),
            "warning": "Employee has no work location / state code — Professional Tax not calculated.",
            "is_verified": False,
        }

    slab = find_pt_slab(db, state_code, gross_earnings, as_of)
    if not slab:
        return {
            "amount": Decimal("0"),
            "warning": f"No Professional Tax slab configured for state '{state_code}' — PT set to 0.",
            "is_verified": False,
        }

    return {
        "amount": Decimal(str(slab.tax_amount)),
        "warning": None if slab.is_verified else (
            f"PT slab for '{state_code}' is not verified against current state law."
        ),
        "is_verified": slab.is_verified,
    }
