"""FORMULA — the calculation is the group's HR-edited definition (lines of
formulas; see services/payroll_engine/formula). For a FORMULA group the
`params` argument of the strategy API is the definition."""
from typing import Any

from app.services.payroll_engine.pay_groups.base import (
    PayGroupCalculationError,
    PayGroupStrategy,
    PayInputs,
    PayResult,
)

FORMULA = "FORMULA"


class FormulaStrategy(PayGroupStrategy):
    calc_model = FORMULA
    description = "HR-defined lines of formulas (earnings, deductions, employer costs) with constants and lookups."
    PARAM_SPEC = {}

    @classmethod
    def param_spec(cls):
        return {}

    @classmethod
    def validate_params(cls, params: Any) -> list[str]:
        from app.services.payroll_engine.formula.compiled import validate_definition
        return validate_definition(params).error_texts()

    @classmethod
    def validate_profile(cls, profile, params: dict) -> list[str]:
        """Employee custom fields against the definition, then a full-month dry
        run with no monthly inputs: any error it raises (missing wage, no
        lookup row for the skill level, ERROR(...)) is reported."""
        from app.services.payroll_engine.formula.compiled import compile_definition
        from app.services.payroll_engine.formula.validation import validate_employee_fields
        compiled = compile_definition(params)
        errors = validate_employee_fields(compiled.definition, getattr(profile, "extra", None) or {})
        if errors:
            return errors
        try:
            cls().calculate(profile, None, PayInputs(), params)
        except PayGroupCalculationError as exc:
            errors.append(str(exc))
        return errors

    def calculate(self, profile, employee, inputs: PayInputs, params: dict) -> PayResult:
        from app.services.payroll_engine.formula.calculator import calculate
        from app.services.payroll_engine.formula.compiled import compile_definition
        result, _ = calculate(compile_definition(params), profile, employee, inputs)
        return result
