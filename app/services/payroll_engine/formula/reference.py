"""GET /pay-groups/formula-reference: everything the formula editor needs to
offer (variables, input codes, functions, operators, tags, choices, rules)."""
from app.services.payroll_engine.formula import catalog as cat
from app.services.payroll_engine.formula.functions import FUNCTIONS
from app.services.payroll_engine.pay_groups.base import AD_HOC_DEDUCTION_LABELS, AD_HOC_EARNING_LABELS

OPERATORS = [
    {"symbol": "+ - * /", "description": "Arithmetic (Decimal, no floating-point error). Division by zero is an error."},
    {"symbol": "%", "description": "Percent, after a number: 12% = 0.12."},
    {"symbol": "( )", "description": "Grouping."},
    {"symbol": "= <> < <= > >=", "description": "Comparisons giving 1 or 0 (== and != also accepted). Text compares "
                                                "case-insensitively. Comparisons cannot be chained."},
    {"symbol": "AND OR NOT", "description": "Logic on non-zero = true; also as functions AND(..), OR(..), NOT(..)."},
    {"symbol": "\"text\"", "description": "Text literal for comparisons and LOOKUP keys (double or single quotes)."},
]

RULES = [
    "Names are case-insensitive and stored upper-case; codes use A-Z, 0-9 and _ (start with a letter, max 40).",
    "A leading '=' is ignored, as in Excel.",
    "Lines are calculated top to bottom. A line can use built-in variables, input codes, constants, employee fields "
    "and any line above it; using a line below it is an error (and a loop is reported as a circular reference).",
    "A line may reuse a built-in variable or input-code name (e.g. DAYS_PRESENT, SUPERVISOR_ALLOWANCE): from that "
    "line on the name means the line. In the line's own formula the name still means the original value.",
    "condition: when it is false the line is left out and reads as 0 in later formulas (ISBLANK(line) is then 1).",
    "omit_if_zero: a line whose value is 0 is left out of the payslip/run (it still reads as 0).",
    "rounding/decimals round the line's result (ROUND = half away from zero, ROUNDUP = away from zero, "
    "ROUNDDOWN = toward zero). Unrounded values keep full precision.",
    "EARNING lines add to gross, DEDUCTION lines to deductions, EMPLOYER lines are employer cost (shown, not "
    "deducted), INFO lines are helpers: shown on the run only when show_on_payslip is true.",
    "Monthly ad-hoc inputs (OTHER_PAYMENT ... LOAN) are paid/deducted as their own lines automatically "
    "(include_ad_hoc_inputs), except an input whose code is also a line code: that line takes it over.",
    "Tags PAID_DAYS / LOP_DAYS / PF_WAGES / ESI_WAGES on INFO lines set those figures on the run line; "
    "defaults: PAID_DAYS, LOP_DAYS, and blank PF/ESI wages.",
    "Net pay is gross - deductions, then net_rounding (a ROUND_OFF earning holds the difference). TDS is added "
    "after the formulas when tds_enabled.",
    "Employee custom fields are stored in the pay profile's `extra` under the field's key (default: its code).",
]


def formula_reference() -> dict:
    return {
        "variables": [
            {"name": v.name, "type": v.type, "source": v.source, "description": v.description,
             "can_be_blank": v.blankable}
            for v in cat.BUILTIN_VARIABLES
        ],
        "input_codes": [
            *({"code": c, "label": label, "kind": "EARNING", "description": "Monthly ad-hoc earning (0 when not "
               "entered; ISBLANK tells)."} for c, label in AD_HOC_EARNING_LABELS.items()),
            *({"code": c, "label": label, "kind": "DEDUCTION", "description": "Monthly ad-hoc deduction (0 when not "
               "entered; ISBLANK tells)."} for c, label in AD_HOC_DEDUCTION_LABELS.items()),
        ],
        "functions": [
            {"name": f.name, "signature": f.signature, "min_args": f.min_args, "max_args": f.max_args,
             "description": f.description, "example": f.example}
            for f in FUNCTIONS.values()
        ],
        "operators": OPERATORS,
        "statutory_tags": [
            {"tag": tag, "kinds": list(kinds), "description": description}
            for tag, (kinds, description) in cat.STATUTORY_TAGS.items()
        ],
        "line_kinds": list(cat.LINE_KINDS),
        "line_roundings": list(cat.LINE_ROUNDINGS),
        "net_roundings": list(cat.NET_ROUNDINGS),
        "attendance_sources": list(cat.ATTENDANCE_SOURCE_CHOICES),
        "employee_field_types": list(cat.FIELD_TYPES),
        "reserved_line_codes": list(cat.RESERVED_LINE_CODES),
        "keywords": list(cat.KEYWORDS),
        "rules": RULES,
    }
