"""EPF (Provident Fund) calculation from the active, versioned PFConfig row.
Nothing here is hardcoded — rates/ceiling/toggle all come from the config.
"""
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.statutory import PFConfig


def get_active_pf_config(db: Session, as_of: date) -> PFConfig | None:
    return (
        db.query(PFConfig)
        .filter(
            PFConfig.is_active.is_(True),
            PFConfig.effective_from <= as_of,
            (PFConfig.effective_to.is_(None)) | (PFConfig.effective_to >= as_of),
        )
        .order_by(PFConfig.effective_from.desc())
        .first()
    )


def calculate_pf(pf_wages: Decimal, config: PFConfig) -> dict:
    """Returns {employee_pf, employer_eps, employer_epf, pf_wages_for_calc}."""
    wage_ceiling = Decimal(str(config.wage_ceiling))
    pf_wages_for_calc = min(pf_wages, wage_ceiling) if config.apply_ceiling else pf_wages

    employee_rate = Decimal(str(config.employee_rate))
    employer_eps_rate = Decimal(str(config.employer_eps_rate))
    employer_epf_rate = Decimal(str(config.employer_epf_rate))

    employee_pf = pf_wages_for_calc * employee_rate
    employer_eps = pf_wages_for_calc * employer_eps_rate
    employer_epf = pf_wages_for_calc * employer_epf_rate

    return {
        "employee_pf": employee_pf,
        "employer_eps": employer_eps,
        "employer_epf": employer_epf,
        "pf_wages_for_calc": pf_wages_for_calc,
    }
