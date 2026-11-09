"""Location/client-contract payroll models ("pay groups"). One strategy per
calc_model; a PayGroup row picks the model and carries all of its numbers
in `params`. See base.py for rounding rules and the monthly-input mapping.
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
from app.services.payroll_engine.pay_groups.monthly_salaried import MonthlySalariedStrategy

STRATEGIES: dict[str, PayGroupStrategy] = {
    s.calc_model: s
    for s in (
        MonthlySalariedStrategy(),
        DailyFlatRateStrategy(),
        DailyWageStandardDaysStrategy(),
        DailyWageAttendanceStrategy(),
        DailyWageBasicDaStrategy(),
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
        key: {"type": f.kind, "description": f.description, **({"choices": list(f.choices)} if f.choices else {})}
        for key, f in get_strategy(calc_model).param_spec().items()
    }
