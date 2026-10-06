"""TDS (income tax withholding) — CLEARLY A BEST-EFFORT STUB.

Decision made for Phase 2 (see the phase report): TDS is implemented as a
simple annualized-average-rate estimate, NEW REGIME ONLY, using whatever
`employee_tax_declarations` / `tds_slabs` / `tds_configs` rows exist for the
employee's financial year. It intentionally excludes: old-regime slabs,
section 89 arrears relief, surcharge tiers, multi-employer income
aggregation, and marginal-relief edge cases around the Section 87A rebate
threshold. This is NOT a compliance-grade calculation — recommend a CA /
compliance sign-off before this number drives any real disbursement.

If no active TDSConfig exists for the run's financial year, TDS silently
resolves to 0 rather than raising, so a payroll run is never blocked by
missing tax-config data in this phase.
"""
from decimal import Decimal

from sqlalchemy.orm import Session

from app.models.statutory import EmployeeTaxDeclaration, TDSConfig, TDSSlab


def _financial_year_for(period_end) -> str:
    year = period_end.year
    if period_end.month >= 4:
        return f"{year}-{str(year + 1)[-2:]}"
    return f"{year - 1}-{str(year)[-2:]}"


def estimate_monthly_tds(
    db: Session,
    employee_id: int,
    annual_gross_estimate: Decimal,
    financial_year: str | None = None,
) -> dict:
    """Very rough monthly TDS estimate: annualize the current month's gross,
    subtract standard deduction + declared 80C/80D-style investments (capped
    crudely, no per-section caps enforced — best-effort only), apply new-regime
    slabs, apply cess, apply Section 87A rebate if under the limit, divide by 12.
    Returns {monthly_tds, note}.
    """
    fy = financial_year
    config = None
    if fy:
        config = db.query(TDSConfig).filter(TDSConfig.financial_year == fy, TDSConfig.is_active.is_(True)).first()
    if config is None:
        config = db.query(TDSConfig).filter(TDSConfig.is_active.is_(True)).order_by(TDSConfig.id.desc()).first()

    if config is None:
        return {"monthly_tds": Decimal("0"), "note": "No active TDS config found — TDS not calculated (0)."}

    declaration = (
        db.query(EmployeeTaxDeclaration)
        .filter(
            EmployeeTaxDeclaration.employee_id == employee_id,
            EmployeeTaxDeclaration.financial_year == config.financial_year,
        )
        .first()
    )

    # New regime only in this phase — old-regime declarations are ignored for
    # the TDS estimate (best-effort stub, see module docstring).
    if declaration is not None and declaration.regime_choice == "Old":
        return {
            "monthly_tds": Decimal("0"),
            "note": "Employee has declared the Old regime; this phase's TDS estimator supports New regime only — TDS not calculated (0).",
        }

    standard_deduction = Decimal(str(config.standard_deduction))
    taxable_income = annual_gross_estimate - standard_deduction
    if taxable_income < 0:
        taxable_income = Decimal("0")

    slabs = (
        db.query(TDSSlab)
        .filter(TDSSlab.regime == "New", TDSSlab.financial_year == config.financial_year)
        .order_by(TDSSlab.income_from.asc())
        .all()
    )
    if not slabs:
        return {"monthly_tds": Decimal("0"), "note": "No New-regime TDS slabs configured — TDS not calculated (0)."}

    tax = Decimal("0")
    for slab in slabs:
        income_from = Decimal(str(slab.income_from))
        income_to = Decimal(str(slab.income_to)) if slab.income_to is not None else None
        rate = Decimal(str(slab.rate))
        if taxable_income <= income_from:
            continue
        slab_top = income_to if income_to is not None else taxable_income
        slab_amount = max(Decimal("0"), min(taxable_income, slab_top) - income_from)
        tax += slab_amount * rate

    rebate_limit = Decimal(str(config.rebate_87a_income_limit))
    rebate_max = Decimal(str(config.rebate_87a_max_amount))
    if taxable_income <= rebate_limit:
        tax = max(Decimal("0"), tax - rebate_max)

    cess_rate = Decimal(str(config.cess_rate))
    tax_with_cess = tax * (Decimal("1") + cess_rate)

    monthly_tds = (tax_with_cess / Decimal("12")).quantize(Decimal("1"))
    return {"monthly_tds": monthly_tds, "note": "Best-effort New-regime annualized estimate — not compliance-grade."}
