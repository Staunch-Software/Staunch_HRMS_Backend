"""params -> formula definition for the five legacy strategies, so a group
converted to FORMULA keeps HR's current rates. Each definition reproduces its
strategy line for line (codes, labels, order, amounts); see
tests/test_formula_equivalence.py."""
import re

from app.services.payroll_engine.pay_groups import get_strategy, with_param_defaults

SETTING_KEYS = ("net_rounding", "tds_enabled", "attendance_source", "pf_applicable_default", "esi_applicable_default")
SKILL_LOOKUPS = {
    "allowance_per_day_by_skill": ("ALLOWANCE_PER_DAY", "Allowance per day"),
    "transport_per_day_by_skill": ("TRANSPORT_PER_DAY", "Transport per day"),
    "basic_per_day_by_skill": ("BASIC_PER_DAY", "Basic per day"),
    "da_per_day_by_skill": ("DA_PER_DAY", "DA per day"),
}
_EXAMPLE = re.compile(r"\s*\([^)]*\d[^)]*\)")


def line(code, name, kind, formula, rounding="NONE", show=None, tag=None, cond=None, omit=False) -> dict:
    out = {"code": code, "name": name, "kind": kind, "formula": formula, "rounding": rounding, "decimals": 0,
           "show_on_payslip": (kind != "INFO") if show is None else show, "statutory_tag": tag,
           "condition": cond, "omit_if_zero": omit}
    return out


def info(code, name, formula, rounding="NONE", **kw) -> dict:
    return line(code, name, "INFO", formula, rounding, show=True, **kw)


def helper(code, name, formula, rounding="NONE", **kw) -> dict:
    return line(code, name, "INFO", formula, rounding, show=False, **kw)


def base_definition(calc_model: str, params: dict, code: str | None = None) -> dict:
    """Settings, constants (every number param) and lookups (every skill table)."""
    p = with_param_defaults(calc_model, params, code)
    spec = get_strategy(calc_model).PARAM_SPEC
    constants, lookups = [], []
    for key, fld in spec.items():
        if key == "standard_days":
            continue
        if fld.kind == "skill_table":
            table = dict(p[key])
            lookup_code, label = SKILL_LOOKUPS[key]
            lookups.append({
                "code": lookup_code, "label": label, "key_label": "Skill level",
                "rows": [{"key": k, "value": v} for k, v in table.items() if k != "DEFAULT"],
                "default": table.get("DEFAULT"),
            })
        else:
            constants.append({"code": key.upper(), "label": _EXAMPLE.sub("", fld.description), "value": p[key]})
    return {
        "version": 1, "standard_days": p["standard_days"], "include_ad_hoc_inputs": True,
        **{k: p[k] for k in SETTING_KEYS}, "constants": constants, "lookups": lookups, "employee_fields": [],
    }


def monthly_salaried(params: dict, code=None) -> dict:
    d = base_definition("MONTHLY_SALARIED", params, code)
    prorate = "* WORKING_DAYS / STANDARD_DAYS"
    d["lines"] = [
        info("MONTHLY_SALARY", "Monthly salary",
             'IF(ISBLANK(MONTHLY_SALARY), ERROR("Pay profile has no monthly_salary."), MONTHLY_SALARY)'),
        info("STATUTORY_GROSS", "Gross for statutory",
             "IF(NOT PF_APPLICABLE AND NOT ESI_APPLICABLE, 0, MONTHLY_SALARY * STATUTORY_GROSS_PCT)", "ROUND"),
        info("WORKING_DAYS", "Working days", "STANDARD_DAYS - LOP_DAYS", tag="PAID_DAYS"),
        helper("STIPEND_FULL", "Stipend (full month)", "IF(STATUTORY_GROSS = 0, MONTHLY_SALARY, 0)"),
        helper("BASIC_DA_FULL", "Basic & DA (full month)", "STATUTORY_GROSS * BASIC_DA_PCT", "ROUND"),
        helper("HRA_FULL", "HRA (full month)", "STATUTORY_GROSS * HRA_PCT", "ROUND"),
        helper("SPECIAL_FULL", "Special allowance (full month)",
               "IF(STIPEND_FULL = 0, MONTHLY_SALARY - BASIC_DA_FULL - HRA_FULL, 0)"),
        line("STIPEND", "Stipend / consultant fees", "EARNING", f"STIPEND_FULL {prorate}", "ROUND", omit=True),
        line("BASIC_DA", "Basic & DA", "EARNING", f"BASIC_DA_FULL {prorate}", "ROUND", tag="BASIC"),
        line("HRA", "HRA", "EARNING", f"HRA_FULL {prorate}", "ROUND", tag="HRA"),
        line("SPL_ALLOWANCE", "Special allowance", "EARNING", f"SPECIAL_FULL {prorate}", "ROUND", tag="SPECIAL"),
        line("OT", "Overtime", "EARNING",
             "IF(STATUTORY_GROSS <> 0, STATUTORY_GROSS, STIPEND_FULL) * OT_DAYS / STANDARD_DAYS", "ROUND",
             tag="OT", omit=True),
        line("PF_EE", "Employee PF", "DEDUCTION",
             "MIN(BASIC_DA * EMPLOYEE_PF_RATE, IF(ISBLANK(PF_CAP), EMPLOYEE_PF_CAP_DEFAULT, PF_CAP))", "ROUNDUP",
             tag="PF_EE", cond="PF_APPLICABLE"),
        helper("PF_WAGE", "PF wages", "BASIC_DA", tag="PF_WAGES", cond="PF_APPLICABLE"),
        line("ESI_EE", "Employee ESI", "DEDUCTION", "(BASIC_DA + HRA + OT) * EMPLOYEE_ESI_RATE", "ROUNDUP",
             tag="ESI_EE", cond="ESI_APPLICABLE"),
        helper("ESI_WAGE", "ESI wages", "BASIC_DA + HRA + OT", tag="ESI_WAGES", cond="ESI_APPLICABLE"),
        line("ESI_ER", "Employer ESI", "EMPLOYER", "STATUTORY_GROSS * EMPLOYER_ESI_RATE", "ROUNDUP",
             tag="ESI_ER", cond="ESI_APPLICABLE"),
        line("PF_ER", "Employer PF", "EMPLOYER",
             "MIN(STATUTORY_GROSS * BASIC_DA_PCT * EMPLOYER_PF_RATE, EMPLOYER_PF_CAP)"
             " + STATUTORY_GROSS * BASIC_DA_PCT * EMPLOYER_PF_ADMIN_RATE", "ROUNDUP",
             tag="PF_ER", cond="PF_APPLICABLE"),
        info("MONTHLY_CTC", "Monthly CTC", "MONTHLY_SALARY + ESI_ER + PF_ER", tag="CTC"),
    ]
    return d


def daily_flat_rate(params: dict, code=None) -> dict:
    d = base_definition("DAILY_FLAT_RATE", params, code)
    d["employee_fields"] = [
        {"code": "BASE_DAY_RATE", "label": "Base day rate (when the pay profile has no daily wage)",
         "type": "number", "required": False},
        {"code": "STANDING_SUPERVISOR_ALLOWANCE", "key": "supervisor_allowance",
         "label": "Standing supervisor allowance (a SUPERVISOR_ALLOWANCE monthly input replaces it)",
         "type": "number", "required": False},
    ]
    d["lines"] = [
        helper("BASE_RATE", "Base day rate",
               'IF(ISBLANK(DAY_WAGE), IF(ISBLANK(BASE_DAY_RATE), '
               'ERROR("Pay profile has no daily_wage (base day rate)."), BASE_DAY_RATE), DAY_WAGE)'),
        info("PER_DAY_SALARY", "Per-day salary", "BASE_RATE + PER_DAY_ALLOWANCE"),
        info("DAYS_PRESENT", "Days present", "DAYS_PRESENT", tag="PAID_DAYS"),
        line("GROSS_SALARY", "Gross salary", "EARNING", "DAYS_PRESENT * PER_DAY_SALARY", "ROUND"),
        line("PF_EE", "PF", "DEDUCTION", "PF_FLAT_BASE * PF_RATE", "ROUND", tag="PF_EE",
             cond="PF_APPLICABLE AND GROSS_SALARY > 0"),
        helper("PF_WAGE", "PF wages", "PF_FLAT_BASE", tag="PF_WAGES", cond="PF_APPLICABLE AND GROSS_SALARY > 0"),
        line("ESI_EE", "ESI", "DEDUCTION", "GROSS_SALARY * EMPLOYEE_ESI_RATE", "ROUND", tag="ESI_EE",
             cond="ESI_APPLICABLE", omit=True),
        helper("ESI_WAGE", "ESI wages", "GROSS_SALARY", tag="ESI_WAGES", cond="ESI_APPLICABLE"),
        line("SUPERVISOR_ALLOWANCE", "Supervisor allowance", "EARNING",
             "IF(ISBLANK(SUPERVISOR_ALLOWANCE), STANDING_SUPERVISOR_ALLOWANCE, SUPERVISOR_ALLOWANCE)", omit=True),
    ]
    return d
