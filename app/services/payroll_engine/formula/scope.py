"""The names one employee's calculation can read: built-ins, monthly inputs,
group constants, employee fields and (as they are computed) lines."""
from decimal import Decimal
from typing import Any

from app.services.payroll_engine.formula.catalog import INPUT_CODES
from app.services.payroll_engine.formula.compiled import Compiled
from app.services.payroll_engine.formula.definition import is_number, lookup_key
from app.services.payroll_engine.formula.errors import FormulaRuntimeError
from app.services.payroll_engine.pay_groups.base import ZERO, PayInputs, dec, normalize_skill

ONE = Decimal(1)
DEFAULT_DAYS_IN_MONTH = 30  # only when calculating outside a payroll month (unit tests, Excel checks)


def _blank(value: Any) -> bool:
    return value is None or value == ""


def resolve_flag(profile, employee, name: str, default: bool) -> bool:
    """pay profile override > employee record > group default (same as the strategies)."""
    value = getattr(profile, f"{name}_applicable", None)
    if value is None and employee is not None:
        value = getattr(employee, f"{name}_applicable", None)
    if value is None:
        value = default
    return bool(value)


def field_value(profile, field: dict) -> Any:
    """The raw value of an employee custom field in profile.extra (key match is case-insensitive)."""
    extra = getattr(profile, "extra", None) or {}
    key = str(field.get("key") or field["code"])
    if key in extra:
        return extra[key]
    lowered = {str(k).lower(): v for k, v in extra.items()}
    return lowered.get(key.lower())


class Scope:
    def __init__(self, compiled: Compiled, profile, employee, inputs: PayInputs):
        self.compiled = compiled
        self.values: dict[str, Any] = {}
        self.blank: set[str] = set()
        self.used: dict[str, Any] | None = None
        settings = compiled.settings
        std = compiled.standard_days

        def put(name, value, blank=False):
            self.values[name] = value
            if blank:
                self.blank.add(name)

        for name in ("monthly_salary", "daily_wage", "pf_cap", "fixed_monthly_amount"):
            raw = getattr(profile, name, None)
            code = "DAY_WAGE" if name == "daily_wage" else name.upper()
            put(code, dec(raw) if not _blank(raw) else ZERO, _blank(raw))
        skill = getattr(profile, "skill_level", None)
        put("SKILL", normalize_skill(skill), _blank(skill))
        put("PF_APPLICABLE", ONE if resolve_flag(profile, employee, "pf", settings["pf_applicable_default"]) else ZERO)
        put("ESI_APPLICABLE", ONE if resolve_flag(profile, employee, "esi", settings["esi_applicable_default"]) else ZERO)
        put("DAYS_IN_MONTH", Decimal(getattr(inputs, "days_in_month", None) or DEFAULT_DAYS_IN_MONTH))
        put("STANDARD_DAYS", std)
        lop = inputs.lop_days
        put("LOP_DAYS", lop)
        present = inputs.present_days
        put("DAYS_PRESENT", present if present is not None else std - lop, present is None)
        put("PAID_DAYS", std - lop)
        put("PAID_LEAVE_DAYS", dec(getattr(inputs, "paid_leave_days", None)))
        put("OT_DAYS", inputs.overtime_days)
        put("OT_HOURS", inputs.overtime_hours)
        put("HAS_INPUT", ONE if inputs.has_input_row else ZERO)
        for code in INPUT_CODES:
            if code in inputs.earnings:
                put(code, inputs.earnings[code])
            elif code in inputs.deductions:
                put(code, inputs.deductions[code])
            else:
                put(code, ZERO, True)
        for code, value in compiled.constants.items():
            put(code, value)
        for code, field in compiled.fields.items():
            raw = field_value(profile, field)
            missing = _blank(raw)
            if missing:
                if field.get("default") is not None:
                    raw = field["default"]
                elif field.get("required"):
                    raise FormulaRuntimeError(f"Pay profile has no {field.get('label') or code} ({code}).",
                                              user_message=True)
            put(code, self._field(field, raw), missing)

    @staticmethod
    def _field(field: dict, raw: Any):
        if field.get("type", "number") == "number":
            if _blank(raw):
                return ZERO
            if not is_number(raw):
                raise FormulaRuntimeError(f"Employee field {field['code']} must be a number, got '{raw}'.",
                                          user_message=True)
            return dec(raw)
        return "" if raw is None else str(raw)

    def set_line(self, code: str, value: Decimal, included: bool) -> None:
        self.values[code] = value
        if included:
            self.blank.discard(code)
        else:
            self.blank.add(code)

    def _record(self, name: str, value: Any) -> None:
        if self.used is not None:
            self.used[name] = value

    def value(self, name: str, pos: int):
        if name not in self.values:
            raise FormulaRuntimeError(f"unknown variable '{name}'", pos)
        value = self.values[name]
        self._record(name, value)
        return value

    def is_blank(self, name: str, pos: int) -> bool:
        if name not in self.values:
            raise FormulaRuntimeError(f"unknown variable '{name}'", pos)
        blank = name in self.blank
        self._record(f"ISBLANK({name})", 1 if blank else 0)
        return blank

    def lookup(self, table: str, key: Any, pos: int, has_default: bool):
        lookup = self.compiled.lookups.get(table)
        if lookup is None:
            raise FormulaRuntimeError(f"unknown lookup table '{table}'", pos)
        norm = lookup_key(key)
        if norm in lookup.rows:
            value = lookup.rows[norm]
        elif has_default:
            return None
        elif lookup.default is not None:
            value = lookup.default
        else:
            configured = ", ".join(sorted(lookup.rows)) or "none"
            shown = key if key not in ("", None) else "(blank)"
            raise FormulaRuntimeError(
                f"No {lookup.label} for {lookup.key_label.lower()} '{shown}' (configured: {configured}).", pos)
        self._record(f"{table}[{key}]", value)
        return value
