"""Location/client-contract payroll models ("pay groups"). One strategy per
calc_model. Every group now runs as FORMULA: its `definition` holds HR-edited
lines of formulas (services/payroll_engine/formula). The five fixed
strategies remain as the source of the params -> definition converters and
for the equivalence tests. See base.py for rounding rules and the
monthly-input mapping.
"""
from app.services.payroll_engine.pay_groups.base import (  # noqa: F401
    PayGroupCalculationError,
    PayGroupParamsError,
    PayGroupStrategy,
    PayInputs,
    PayProfileData,
    PayResult,
)
from app.services.payroll_engine.pay_groups.daily_flat import DailyFlatRateStrategy
from app.services.payroll_engine.pay_groups.daily_wage_attendance import DailyWageAttendanceStrategy
from app.services.payroll_engine.pay_groups.daily_wage_basic_da import DailyWageBasicDaStrategy
from app.services.payroll_engine.pay_groups.daily_wage_standard_days import DailyWageStandardDaysStrategy
from app.services.payroll_engine.pay_groups.formula_strategy import FORMULA, FormulaStrategy
from app.services.payroll_engine.pay_groups.monthly_salaried import MonthlySalariedStrategy

# The five fixed strategies are kept as the converter source and for the
# equivalence tests; groups themselves run as FORMULA.
STRATEGIES: dict[str, PayGroupStrategy] = {
    s.calc_model: s
    for s in (
        MonthlySalariedStrategy(),
        DailyFlatRateStrategy(),
        DailyWageStandardDaysStrategy(),
        DailyWageAttendanceStrategy(),
        DailyWageBasicDaStrategy(),
        FormulaStrategy(),
    )
}


def get_strategy(calc_model: str) -> PayGroupStrategy:
    try:
        return STRATEGIES[calc_model]
    except KeyError:
        raise PayGroupParamsError(
            f"Unknown calc_model '{calc_model}' (known: {', '.join(sorted(STRATEGIES))})"
        ) from None


def validate_params(calc_model: str, params) -> list[str]:
    return get_strategy(calc_model).validate_params(params)


def param_spec(calc_model: str) -> dict[str, dict]:
    return {
        key: {
            "type": f.kind,
            "description": f.description,
            **({"choices": list(f.choices)} if f.choices else {}),
            **({"default": f.default} if f.default is not None else {}),
        }
        for key, f in get_strategy(calc_model).param_spec().items()
    }


# Defaults that differ by group code for keys the stored row lacks.
GROUP_PARAM_DEFAULTS: dict[str, dict] = {"CHENNAI_HO": {"attendance_source": "BIOMETRIC"}}


def with_param_defaults(calc_model: str, params: dict, code: str | None = None) -> dict:
    """params with every defaulted key the stored row lacks filled in."""
    defaults = {key: f.default for key, f in get_strategy(calc_model).param_spec().items() if f.default is not None}
    return {**defaults, **GROUP_PARAM_DEFAULTS.get(code or "", {}), **(params or {})}


def group_config(group) -> dict:
    """What the group's strategy takes as `params`: the definition for a
    FORMULA group, else the params."""
    if group.calc_model == FORMULA:
        return group.definition or {}
    return group.params or {}


def group_setting(group, key: str, default=None):
    """tds_enabled / net_rounding / attendance_source ... for either kind of group."""
    value = group_config(group).get(key)
    return default if value is None else value


def calculate_for_group(group, profile, employee, inputs):
    return get_strategy(group.calc_model).calculate(profile, employee, inputs, group_config(group))


def validate_profile_for_group(group, profile) -> list[str]:
    try:
        return get_strategy(group.calc_model).validate_profile(profile, group_config(group))
    except PayGroupParamsError as exc:
        return [str(exc)]


def extract_params(group) -> dict:
    """Params for the Excel row mappers: a FORMULA group's constants (lower-case
    keys) over the legacy params it was converted from."""
    if group.calc_model != FORMULA:
        return group.params or {}
    constants = {str(c.get("code", "")).lower(): c.get("value") for c in (group.definition or {}).get("constants", [])}
    return {**(group.params or {}), **constants}


def present_days_from_calendar(group) -> bool:
    """True when the group pays on LOP (monthly salary): attendance sends
    calendar days - LOP as present days. A FORMULA group counts as such when
    no formula uses DAYS_PRESENT."""
    if group.calc_model != FORMULA:
        return group.calc_model == "MONTHLY_SALARIED"
    from app.services.payroll_engine.formula.compiled import validate_definition
    from app.services.payroll_engine.formula.references import _names
    result = validate_definition(group.definition or {})
    return not any("DAYS_PRESENT" in _names(trees) for trees in result.parsed.values())


def attendance_source_of(group) -> str:
    """BIOMETRIC | MUSTER | INPUT for a PayGroup row (defaults applied)."""
    value = group_config(group).get("attendance_source")
    if value:
        return value
    return GROUP_PARAM_DEFAULTS.get(group.code, {}).get("attendance_source", "INPUT")
