"""Day-wage strategies -> formula definitions, plus the converter dispatch."""
from app.services.payroll_engine.formula.converters import (
    base_definition,
    daily_flat_rate,
    helper,
    info,
    line,
    monthly_salaried,
)


def daily_wage_standard_days(params: dict, code=None) -> dict:
    d = base_definition("DAILY_WAGE_STANDARD_DAYS", params, code)
    prorate = "* DAYS_PRESENT / STANDARD_DAYS"
    d["lines"] = [
        helper("WAGE", "Day wage", 'IF(ISBLANK(DAY_WAGE), ERROR("Pay profile has no daily_wage."), DAY_WAGE)'),
        info("MONTHLY_GROSS", "Monthly gross (wage x standard days)", "WAGE * STANDARD_DAYS"),
        info("DAYS_PRESENT", "Days present", "DAYS_PRESENT", tag="PAID_DAYS"),
        helper("LOP", "LOP days", "STANDARD_DAYS - DAYS_PRESENT", tag="LOP_DAYS"),
        helper("BASIC_FULL", "Basic (full month)", "MONTHLY_GROSS * BASIC_PCT"),
        helper("DA_FULL", "DA (full month)", "MONTHLY_GROSS * DA_PCT"),
        helper("HRA_FULL", "HRA (full month)", "BASIC_FULL * HRA_PCT_OF_BASIC"),
        helper("SPECIAL_FULL", "Special allowance (full month)", "SPECIAL_ALLOWANCE_PER_DAY * STANDARD_DAYS"),
        helper("PRORATED_GROSS", "Prorated gross (bonus base)", f"MONTHLY_GROSS {prorate}", "ROUNDUP"),
        line("BASIC", "Basic", "EARNING", f"BASIC_FULL {prorate}", "ROUND", tag="BASIC"),
        line("DA", "DA", "EARNING", f"DA_FULL {prorate}", "ROUND", tag="DA"),
        line("HRA", "HRA", "EARNING", f"HRA_FULL {prorate}", "ROUND", tag="HRA"),
        line("SPL_ALLOWANCE", "Special allowance", "EARNING", f"SPECIAL_FULL {prorate}", "ROUND", tag="SPECIAL"),
        helper("TOTAL_GROSS", "Total gross", "BASIC + DA + HRA + SPL_ALLOWANCE"),
        line("OT", "Overtime", "EARNING", "WAGE * OT_MULTIPLIER / OT_HOURS_PER_DAY * OT_HOURS", tag="OT",
             cond="OT_HOURS <> 0"),
        line("BONUS", "Bonus (prorated)", "EARNING", "MIN(PRORATED_GROSS * BONUS_RATE, BONUS_WAGE_CEILING * BONUS_RATE)",
             tag="BONUS"),
        line("PF_EE", "Employee PF", "DEDUCTION", "MIN(TOTAL_GROSS * EMPLOYEE_PF_RATE, EMPLOYEE_PF_CAP)", "ROUNDUP",
             tag="PF_EE", cond="PF_APPLICABLE"),
        helper("PF_WAGE", "PF wages", "TOTAL_GROSS", tag="PF_WAGES", cond="PF_APPLICABLE"),
        line("PF_ER", "Employer PF", "EMPLOYER", "EMPLOYER_PF_WAGE_BASE * EMPLOYER_PF_RATE", tag="PF_ER"),
        info("TRANSPORT_CTC", "Transport (CTC)", "TRANSPORT_PER_DAY * STANDARD_DAYS"),
        info("BONUS_CTC", "Bonus (CTC)", "CTC_BONUS_WAGE_BASE * BONUS_RATE"),
        info("MONTHLY_CTC", "Monthly CTC",
             "BASIC_FULL + DA_FULL + HRA_FULL + SPECIAL_FULL + TRANSPORT_CTC + PF_ER + BONUS_CTC", "ROUND", tag="CTC"),
    ]
    return d


def daily_wage_attendance(params: dict, code=None) -> dict:
    d = base_definition("DAILY_WAGE_ATTENDANCE", params, code)
    by_days = "ISBLANK(FIXED_MONTHLY_AMOUNT)"
    d["lines"] = [
        helper("PROFILE_CHECK", "Profile check",
               'IF(ISBLANK(DAY_WAGE) AND ISBLANK(FIXED_MONTHLY_AMOUNT), '
               'ERROR("daily_wage or fixed_monthly_amount is required"), 0)'),
        info("DAYS_PRESENT", "Days present", "DAYS_PRESENT", tag="PAID_DAYS"),
        line("FIXED_AMOUNT", "Fixed monthly amount", "EARNING", "FIXED_MONTHLY_AMOUNT", cond=f"NOT {by_days}"),
        line("WAGES", "Wages", "EARNING", "DAY_WAGE * DAYS_PRESENT", tag="BASIC", cond=by_days),
        line("ALLOWANCE", "Allowance (25% of wage 2016)", "EARNING", "LOOKUP(ALLOWANCE_PER_DAY, SKILL) * DAYS_PRESENT",
             cond=by_days),
        line("BONUS", "Bonus (8.33% of wage)", "EARNING", "MIN(WAGES * BONUS_RATE, BONUS_WAGE_CEILING * BONUS_RATE)",
             tag="BONUS", cond=by_days),
        line("TRANSPORT", "Transport", "EARNING", "LOOKUP(TRANSPORT_PER_DAY, SKILL) * DAYS_PRESENT", cond=by_days,
             omit=True),
        line("OT", "Overtime", "EARNING", "DAY_WAGE / OT_HOURS_PER_DAY * OT_MULTIPLIER * OT_HOURS", tag="OT",
             cond="NOT ISBLANK(DAY_WAGE) AND OT_HOURS <> 0"),
        helper("PF_WAGE", "PF wages", "IF(ISBLANK(DAY_WAGE), FIXED_MONTHLY_AMOUNT, DAY_WAGE * DAYS_PRESENT)",
               tag="PF_WAGES", cond="PF_APPLICABLE"),
        line("PF_EE", "PF", "DEDUCTION", "IF(PF_WAGE > PF_THRESHOLD, PF_FLAT_BASE * PF_RATE, PF_WAGE * PF_RATE)",
             tag="PF_EE", cond="PF_APPLICABLE"),
        info("MONTHLY_CTC", "CTC",
             f"IF({by_days}, DAY_WAGE * DAYS_PRESENT, FIXED_MONTHLY_AMOUNT) + ALLOWANCE + BONUS + CTC_FIXED_ADDON",
             tag="CTC"),
    ]
    return d


def daily_wage_basic_da(params: dict, code=None) -> dict:
    d = base_definition("DAILY_WAGE_BASIC_DA", params, code)
    d["lines"] = [
        helper("WAGE", "Day wage", 'IF(ISBLANK(DAY_WAGE), ERROR("Pay profile has no daily_wage."), DAY_WAGE)'),
        info("MONTHLY_GROSS", "Monthly gross (wage x standard days)", "WAGE * STANDARD_DAYS"),
        info("DAYS_PAID", "Days paid", "DAYS_PRESENT", tag="PAID_DAYS"),
        helper("LOP", "LOP days", "STANDARD_DAYS - DAYS_PAID", tag="LOP_DAYS"),
        helper("THIS_MONTH", "This month's gross", "MONTHLY_GROSS * DAYS_PAID / STANDARD_DAYS", "ROUNDUP"),
        line("BASIC", "Basic", "EARNING", "LOOKUP(BASIC_PER_DAY, SKILL) * DAYS_PAID", tag="BASIC"),
        line("DA", "DA", "EARNING", "LOOKUP(DA_PER_DAY, SKILL) * DAYS_PAID", tag="DA"),
        line("SPL_ALLOWANCE", "Special allowance", "EARNING", "THIS_MONTH - (BASIC + DA)", tag="SPECIAL"),
        helper("GROSS", "Gross", "BASIC + DA + SPL_ALLOWANCE"),
        line("PF_EE", "Employee PF", "DEDUCTION", "MIN(BASIC + DA, PF_WAGE_CEILING) * PF_RATE", "ROUND", tag="PF_EE",
             cond="PF_APPLICABLE"),
        line("PF_ER", "Employer PF", "EMPLOYER", "PF_EE", tag="PF_ER", cond="PF_APPLICABLE"),
        helper("PF_WAGE", "PF wages", "BASIC + DA", tag="PF_WAGES", cond="PF_APPLICABLE"),
        line("ESI_EE", "Employee ESI", "DEDUCTION", "GROSS * EMPLOYEE_ESI_RATE", tag="ESI_EE", cond="ESI_APPLICABLE"),
        line("ESI_ER", "Employer ESI", "EMPLOYER", "GROSS * EMPLOYER_ESI_RATE", tag="ESI_ER", cond="ESI_APPLICABLE"),
        helper("ESI_WAGE", "ESI wages", "GROSS", tag="ESI_WAGES", cond="ESI_APPLICABLE"),
        line("PT", "Professional tax", "DEDUCTION", "PROFESSIONAL_TAX_MONTHLY", tag="PT", omit=True),
        line("INSURANCE_ER", "Insurance (employer)", "EMPLOYER", "EMPLOYER_INSURANCE_MONTHLY", omit=True),
        info("BONUS_CTC", "Bonus (CTC)", "MONTHLY_GROSS * BONUS_RATE", "ROUND"),
        info("MONTHLY_CTC", "CTC", "GROSS + PF_ER + ESI_ER + INSURANCE_ER + BONUS_CTC", "ROUND", tag="CTC"),
    ]
    return d


CONVERTERS = {
    "MONTHLY_SALARIED": monthly_salaried,
    "DAILY_FLAT_RATE": daily_flat_rate,
    "DAILY_WAGE_STANDARD_DAYS": daily_wage_standard_days,
    "DAILY_WAGE_ATTENDANCE": daily_wage_attendance,
    "DAILY_WAGE_BASIC_DA": daily_wage_basic_da,
}


def definition_from_params(calc_model: str, params: dict, code: str | None = None) -> dict:
    """The FORMULA definition equivalent to `calc_model` run with `params`."""
    try:
        converter = CONVERTERS[calc_model]
    except KeyError:
        raise ValueError(f"No converter for calc_model '{calc_model}'") from None
    return converter(params or {}, code)
