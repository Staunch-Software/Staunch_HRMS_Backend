"""Pay-profile checks for a FORMULA group's employee custom fields."""
from app.services.payroll_engine.formula.definition import field_value_error
from app.services.payroll_engine.formula.scope import field_value


class _Extra:
    def __init__(self, extra: dict):
        self.extra = extra


def validate_employee_fields(definition: dict, extra: dict) -> list[str]:
    errors = []
    holder = _Extra(extra or {})
    for field in definition.get("employee_fields") or []:
        value = field_value(holder, field)
        label = f"{field['code']} ({field.get('label') or field['code']})"
        if value is None or value == "":
            if field.get("required") and field.get("default") is None:
                errors.append(f"{label} is required")
            continue
        problem = field_value_error(field, value)
        if problem:
            errors.append(f"{label} {problem}")
    return errors
