"""Names every pay-group formula can use without defining them, plus the
line kinds / rounding modes / statutory tags a definition may choose from."""
from dataclasses import dataclass

from app.services.payroll_engine.pay_groups.base import (
    AD_HOC_DEDUCTION_LABELS,
    AD_HOC_EARNING_LABELS,
    ATTENDANCE_SOURCES,
    NET_ROUNDING_CHOICES,
)


@dataclass(frozen=True)
class Variable:
    name: str
    type: str  # number | text | flag
    source: str  # profile | employee | attendance | group | input | period
    description: str
    blankable: bool = False


BUILTIN_VARIABLES: tuple[Variable, ...] = (
    Variable("MONTHLY_SALARY", "number", "profile", "Pay profile monthly salary (0 when blank).", True),
    Variable("DAY_WAGE", "number", "profile", "Pay profile daily wage (0 when blank).", True),
    Variable("SKILL", "text", "profile",
             "Pay profile skill level, normalised to upper case with underscores ('Highly Skilled' -> "
             "HIGHLY_SKILLED); empty text when blank.", True),
    Variable("PF_APPLICABLE", "flag", "profile",
             "1 when PF applies: pay profile, else employee record, else the group default; otherwise 0."),
    Variable("ESI_APPLICABLE", "flag", "profile",
             "1 when ESI applies: pay profile, else employee record, else the group default; otherwise 0."),
    Variable("PF_CAP", "number", "profile", "Pay profile employee-PF cap (0 when blank; test with ISBLANK).", True),
    Variable("FIXED_MONTHLY_AMOUNT", "number", "profile", "Pay profile fixed monthly amount (0 when blank).", True),
    Variable("DAYS_IN_MONTH", "number", "period", "Calendar days in the payroll month (28-31)."),
    Variable("STANDARD_DAYS", "number", "group", "The group's standard days per month (definition.standard_days)."),
    Variable("DAYS_PRESENT", "number", "attendance",
             "Days present from the monthly input; when not entered, STANDARD_DAYS - LOP_DAYS "
             "(ISBLANK(DAYS_PRESENT) tells the two apart).", True),
    Variable("LOP_DAYS", "number", "attendance", "Loss-of-pay days from the monthly input (0 when none)."),
    Variable("PAID_DAYS", "number", "attendance",
             "STANDARD_DAYS - LOP_DAYS: the paid days for monthly-salary (LOP) logic. Day-wage logic usually "
             "uses DAYS_PRESENT instead."),
    Variable("PAID_LEAVE_DAYS", "number", "attendance", "Paid leave days from the monthly input."),
    Variable("OT_DAYS", "number", "attendance", "Overtime counted in days from the monthly input."),
    Variable("OT_HOURS", "number", "attendance", "Overtime hours from the monthly input."),
    Variable("HAS_INPUT", "flag", "attendance",
             "1 when a monthly input row exists for the employee and month, else 0 (then DAYS_PRESENT is the "
             "full standard month and a warning is added)."),
)
BUILTIN_BY_NAME = {v.name: v for v in BUILTIN_VARIABLES}

INPUT_EARNING_CODES = tuple(AD_HOC_EARNING_LABELS)
INPUT_DEDUCTION_CODES = tuple(AD_HOC_DEDUCTION_LABELS)
INPUT_CODES = INPUT_EARNING_CODES + INPUT_DEDUCTION_CODES

LINE_KINDS = ("EARNING", "DEDUCTION", "EMPLOYER", "INFO")
LINE_ROUNDINGS = ("NONE", "ROUND", "ROUNDUP", "ROUNDDOWN")
FIELD_TYPES = ("number", "text", "select")
NET_ROUNDINGS = NET_ROUNDING_CHOICES
ATTENDANCE_SOURCE_CHOICES = ATTENDANCE_SOURCES

# tag -> (line kinds allowed, description)
STATUTORY_TAGS: dict[str, tuple[tuple[str, ...], str]] = {
    "BASIC": (("EARNING",), "Basic pay (or Basic & DA)"),
    "DA": (("EARNING",), "Dearness allowance"),
    "HRA": (("EARNING",), "House rent allowance"),
    "SPECIAL": (("EARNING",), "Special allowance"),
    "OT": (("EARNING",), "Overtime pay"),
    "BONUS": (("EARNING",), "Statutory bonus paid monthly"),
    "PF_EE": (("DEDUCTION",), "Employee provident fund"),
    "ESI_EE": (("DEDUCTION",), "Employee ESI"),
    "PT": (("DEDUCTION",), "Professional tax"),
    "LWF_EE": (("DEDUCTION",), "Employee labour welfare fund"),
    "PF_ER": (("EMPLOYER",), "Employer provident fund (cost only)"),
    "ESI_ER": (("EMPLOYER",), "Employer ESI (cost only)"),
    "LWF_ER": (("EMPLOYER",), "Employer labour welfare fund (cost only)"),
    "PF_WAGES": (("INFO",), "PF wages reported on the run line (blank when the line's condition is false)"),
    "ESI_WAGES": (("INFO",), "ESI wages reported on the run line (blank when the line's condition is false)"),
    "PAID_DAYS": (("INFO",), "Paid days reported on the run line (default: PAID_DAYS)"),
    "LOP_DAYS": (("INFO",), "LOP days reported on the run line (default: LOP_DAYS)"),
    "CTC": (("INFO",), "Monthly cost to company"),
}
RESULT_TAGS = ("PF_WAGES", "ESI_WAGES", "PAID_DAYS", "LOP_DAYS")

# Codes the payroll run itself writes: a line cannot use them.
RESERVED_LINE_CODES = ("ROUND_OFF", "TDS")
KEYWORDS = ("AND", "OR", "NOT", "TRUE", "FALSE")
