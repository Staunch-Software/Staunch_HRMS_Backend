"""Income tax deducted at source on salary, projected over the financial year
(April-March).

    annual projection = YTD gross (approved/paid runs this FY, before this month)
                        + this month's gross x months left (this month to March)
                        + previous employer's income
    taxable           = projection - standard deduction
                        (- HR-entered old_regime_deductions, Old regime only)
    monthly TDS       = (annual tax - YTD TDS - previous employer's TDS) / months left

Slabs, standard deduction, Section 87A rebate, surcharge bands and cess are
data in tds_slabs / tds_configs per (financial_year, regime). The regime is
the employee's EmployeeTaxDeclaration for the year, New when there is none.

Not modelled: higher old-regime exemption limits for senior citizens,
Section 89 arrears relief, perquisites and non-salary income. Annual tax and
the monthly amount are rounded per Rounding Rules (whole rupee, or nearest
Rs 10 under section 288B). A missing config or slab set resolves to 0 with a
warning so a payroll run is never blocked.
"""
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy.orm import Session

from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.salary_component import SalaryComponent
from app.models.statutory import EmployeeTaxDeclaration, TDSConfig, TDSSlab
from app.services.payroll_settings import round_tds, rounding_settings

ZERO = Decimal("0")
ONE = Decimal("1")
PAISE = Decimal("0.01")

REGIMES = ("New", "Old")
DEFAULT_REGIME = "New"
# Runs whose pay counts as already paid (and whose TDS as already deducted).
YTD_RUN_STATUSES = ("Approved", "Paid")
# Breakdown code of the pay-group TDS line; component-engine TDS lines use the
# code of any salary component with statutory_type TDS.
TDS_CODE = "TDS"


def _d(value) -> Decimal:
    if value is None or value == "":
        return ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return ZERO


def _q(value: Decimal) -> Decimal:
    return value.quantize(PAISE, rounding=ROUND_HALF_UP)


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def financial_year_for(period_end: date) -> str:
    year = period_end.year if period_end.month >= 4 else period_end.year - 1
    return f"{year}-{str(year + 1)[-2:]}"


def financial_year_start(period_end: date) -> date:
    return date(period_end.year if period_end.month >= 4 else period_end.year - 1, 4, 1)


def months_remaining_in_fy(period_end: date) -> int:
    """Months from period_end's month through March, inclusive."""
    return 16 - period_end.month if period_end.month >= 4 else 4 - period_end.month


# --- annual tax (pure) -------------------------------------------------------

def _slab_parts(slab) -> tuple[Decimal, Decimal | None, Decimal]:
    if isinstance(slab, (tuple, list)):
        lo, hi, rate = slab
    else:
        lo, hi, rate = _get(slab, "income_from"), _get(slab, "income_to"), _get(slab, "rate")
    return _d(lo), (None if hi is None else _d(hi)), _d(rate)


def _slab_tax(income: Decimal, slabs) -> Decimal:
    tax = ZERO
    for slab in slabs:
        lo, hi, rate = _slab_parts(slab)
        if income <= lo:
            continue
        top = income if hi is None else min(income, hi)
        tax += (top - lo) * rate
    return tax


def _tax_before_surcharge(income: Decimal, config, slabs) -> tuple[Decimal, Decimal, Decimal]:
    """(slab tax, 87A rebate, 87A marginal relief). New regime only: above the
    rebate limit, tax cannot exceed the income above that limit."""
    tax = _slab_tax(income, slabs)
    limit = _d(_get(config, "rebate_87a_income_limit"))
    rebate = relief = ZERO
    if income <= limit:
        rebate = min(tax, _d(_get(config, "rebate_87a_max_amount")))
    elif (_get(config, "regime") or DEFAULT_REGIME) == "New":
        relief = max(ZERO, tax - (income - limit))
    return tax, rebate, relief


def _surcharge_band(income: Decimal, bands) -> tuple[Decimal, Decimal, Decimal] | None:
    """(threshold, rate, rate of the band below) for the highest band whose
    threshold the income exceeds."""
    ordered = sorted(((_d(_get(b, "above")), _d(_get(b, "rate"))) for b in bands or []), key=lambda b: b[0])
    chosen, previous_rate = None, ZERO
    for threshold, rate in ordered:
        if income > threshold:
            previous_rate = chosen[1] if chosen else ZERO
            chosen = (threshold, rate)
    return (chosen[0], chosen[1], previous_rate) if chosen else None


def compute_annual_tax(taxable_income, regime_config, slabs) -> dict:
    """Annual tax on `taxable_income` (after standard deduction). regime_config
    is a TDSConfig or a dict with the same keys (regime, rebate_87a_income_limit,
    rebate_87a_max_amount, cess_rate, surcharge_bands); slabs are TDSSlab rows,
    dicts or (income_from, income_to, rate) tuples. `total` is rounded to the
    rupee, the parts to the paisa."""
    income = max(ZERO, _d(taxable_income))
    tax, rebate, relief = _tax_before_surcharge(income, regime_config, slabs)
    net_tax = tax - rebate - relief

    surcharge = surcharge_relief = ZERO
    band = _surcharge_band(income, _get(regime_config, "surcharge_bands"))
    if band:
        threshold, rate, previous_rate = band
        surcharge = net_tax * rate
        t_tax, t_rebate, t_relief = _tax_before_surcharge(threshold, regime_config, slabs)
        cap = (t_tax - t_rebate - t_relief) * (ONE + previous_rate) + (income - threshold)
        if net_tax + surcharge > cap:
            surcharge_relief = net_tax + surcharge - cap
            surcharge -= surcharge_relief

    cess = (net_tax + surcharge) * _d(_get(regime_config, "cess_rate"))
    total = (net_tax + surcharge + cess).quantize(ONE, rounding=ROUND_HALF_UP)
    return {
        "taxable_income": _q(income),
        "tax": _q(tax),
        "rebate": _q(rebate),
        "marginal_relief": _q(relief),
        "surcharge": _q(surcharge),
        "surcharge_marginal_relief": _q(surcharge_relief),
        "cess": _q(cess),
        "total": total,
    }


# --- monthly TDS (DB) ----------------------------------------------------------

def get_regime_rules(db: Session, financial_year: str, regime: str) -> tuple[TDSConfig | None, list[TDSSlab]]:
    config = (
        db.query(TDSConfig)
        .filter(
            TDSConfig.financial_year == financial_year,
            TDSConfig.regime == regime,
            TDSConfig.is_active.is_(True),
        )
        .order_by(TDSConfig.id.desc())
        .first()
    )
    slabs = (
        db.query(TDSSlab)
        .filter(TDSSlab.financial_year == financial_year, TDSSlab.regime == regime)
        .order_by(TDSSlab.income_from.asc())
        .all()
    )
    return config, slabs


def get_declaration(db: Session, employee_id: int, financial_year: str) -> EmployeeTaxDeclaration | None:
    return (
        db.query(EmployeeTaxDeclaration)
        .filter(
            EmployeeTaxDeclaration.employee_id == employee_id,
            EmployeeTaxDeclaration.financial_year == financial_year,
        )
        .first()
    )


def year_to_date(db: Session, employee_id: int, period_end: date, exclude_run_id: int | None = None) -> tuple[Decimal, Decimal]:
    """(gross, TDS) from this employee's included line items in approved/paid
    runs of the same financial year that ended before period_end's month."""
    query = (
        db.query(PayrollRunLineItem)
        .join(PayrollRun, PayrollRunLineItem.payroll_run_id == PayrollRun.id)
        .filter(
            PayrollRunLineItem.employee_id == employee_id,
            PayrollRunLineItem.status == "Included",
            PayrollRun.status.in_(YTD_RUN_STATUSES),
            PayrollRun.period_end >= financial_year_start(period_end),
            PayrollRun.period_end < period_end.replace(day=1),
        )
    )
    if exclude_run_id is not None:
        query = query.filter(PayrollRun.id != exclude_run_id)
    items = query.all()
    if not items:
        return ZERO, ZERO

    tds_codes = {TDS_CODE} | {
        code for (code,) in db.query(SalaryComponent.code).filter(SalaryComponent.statutory_type == "TDS").all()
    }
    gross = tds = ZERO
    for item in items:
        gross += _d(item.gross_earnings)
        for entry in item.computation_breakdown or []:
            if entry.get("component_type") == "Deduction" and entry.get("component_code") in tds_codes:
                tds += _d(entry.get("resolved_amount"))
    return gross, tds


def compute_monthly_tds(db: Session, employee, period_end: date, current_month_gross, run_id: int | None = None) -> dict:
    """This month's TDS for `employee` (an Employee or its id). `amount` is a
    whole-rupee Decimal; the other keys explain how it was reached."""
    employee_id = getattr(employee, "id", employee)
    fy = financial_year_for(period_end)
    months = months_remaining_in_fy(period_end)

    declaration = get_declaration(db, employee_id, fy)
    regime = declaration.regime_choice if declaration and declaration.regime_choice in REGIMES else DEFAULT_REGIME
    previous_income = _d(declaration.previous_employer_income) if declaration else ZERO
    previous_tds = _d(declaration.previous_employer_tds) if declaration else ZERO
    old_deductions = _d(declaration.old_regime_deductions) if declaration and regime == "Old" else ZERO

    ytd_gross, ytd_tds = year_to_date(db, employee_id, period_end, run_id)
    gross = _d(current_month_gross)
    projection = ytd_gross + gross * months + previous_income

    result = {
        "amount": ZERO,
        "financial_year": fy,
        "regime": regime,
        "has_declaration": declaration is not None,
        "months_remaining": months,
        "current_month_gross": _q(gross),
        "ytd_gross": _q(ytd_gross),
        "ytd_tds": _q(ytd_tds),
        "previous_employer_income": _q(previous_income),
        "previous_employer_tds": _q(previous_tds),
        "annual_projection": _q(projection),
        "standard_deduction": ZERO,
        "old_regime_deductions": _q(old_deductions),
        "taxable_income": ZERO,
        "annual_tax": ZERO,
        "tax_breakdown": None,
        "remaining_tax": ZERO,
        "warning": None,
    }

    config, slabs = get_regime_rules(db, fy, regime)
    if config is None or not slabs:
        missing = "config" if config is None else "slabs"
        result["warning"] = f"No active {regime}-regime TDS {missing} for FY {fy} — TDS not calculated (0)."
        return result

    standard_deduction = _d(config.standard_deduction)
    taxable = max(ZERO, projection - standard_deduction - old_deductions)
    tax = compute_annual_tax(taxable, config, slabs)
    mode = rounding_settings(db).tds_rounding
    annual_tax = round_tds(tax["total"], mode)
    remaining = max(ZERO, annual_tax - ytd_tds - previous_tds)

    result.update(
        amount=round_tds(remaining / months, mode),
        standard_deduction=_q(standard_deduction),
        taxable_income=tax["taxable_income"],
        annual_tax=annual_tax,
        tax_breakdown=tax,
        remaining_tax=_q(remaining),
    )
    warnings = []
    if not config.is_verified:
        warnings.append(f"FY {fy} {regime}-regime TDS rules are not marked verified.")
    if ytd_tds + previous_tds > annual_tax:
        warnings.append("TDS already deducted exceeds the projected annual tax — no TDS this month.")
    result["warning"] = " ".join(warnings) or None
    return result
