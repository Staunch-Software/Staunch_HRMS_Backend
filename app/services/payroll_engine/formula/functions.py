"""Function catalogue of the formula language: arity, signature, help text.
Rounding follows Excel: ROUND half away from zero, ROUNDUP away from zero,
ROUNDDOWN toward zero. All maths is Decimal."""
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Optional

from app.services.payroll_engine.pay_groups.base import _clean, xl_round, xl_roundup


@dataclass(frozen=True)
class FunctionSpec:
    name: str
    min_args: int
    max_args: Optional[int]  # None = any number
    signature: str
    description: str
    example: str


FUNCTIONS: dict[str, FunctionSpec] = {f.name: f for f in (
    FunctionSpec("IF", 2, 3, "IF(condition, value_if_true, value_if_false)",
                 "value_if_true when condition is non-zero, else value_if_false (0 when omitted). "
                 "Only the chosen branch is evaluated.", "IF(PF_APPLICABLE, BASIC * PF_RATE, 0)"),
    FunctionSpec("AND", 1, None, "AND(a, b, ...)", "1 when every argument is non-zero, else 0. Also usable as "
                 "'a AND b'.", "AND(PF_APPLICABLE, GROSS > 0)"),
    FunctionSpec("OR", 1, None, "OR(a, b, ...)", "1 when any argument is non-zero, else 0. Also usable as "
                 "'a OR b'.", "OR(SKILL = \"SKILLED\", SKILL = \"HIGHLY_SKILLED\")"),
    FunctionSpec("NOT", 1, 1, "NOT(a)", "1 when a is 0, else 0. Also usable as 'NOT a'.", "NOT ISBLANK(PF_CAP)"),
    FunctionSpec("ROUND", 1, 2, "ROUND(x, decimals=0)",
                 "Excel ROUND: half away from zero (2.5 -> 3, -2.5 -> -3).", "ROUND(BASIC * 12%, 0)"),
    FunctionSpec("ROUNDUP", 1, 2, "ROUNDUP(x, decimals=0)",
                 "Excel ROUNDUP: away from zero (1.01 -> 2, -1.01 -> -2).", "ROUNDUP(GROSS * 0.75%, 0)"),
    FunctionSpec("ROUNDDOWN", 1, 2, "ROUNDDOWN(x, decimals=0)",
                 "Excel ROUNDDOWN: toward zero (1.99 -> 1, -1.99 -> -1).", "ROUNDDOWN(DAYS_PRESENT / 2, 0)"),
    FunctionSpec("CEILING", 1, 2, "CEILING(x, multiple=1)",
                 "Smallest multiple of `multiple` that is >= x (multiple must be > 0).", "CEILING(NET, 10)"),
    FunctionSpec("FLOOR", 1, 2, "FLOOR(x, multiple=1)",
                 "Largest multiple of `multiple` that is <= x (multiple must be > 0).", "FLOOR(OT_HOURS, 0.5)"),
    FunctionSpec("MIN", 1, None, "MIN(a, b, ...)", "Smallest argument.", "MIN(BASIC * 12%, 1800)"),
    FunctionSpec("MAX", 1, None, "MAX(a, b, ...)", "Largest argument.", "MAX(GROSS - 15000, 0)"),
    FunctionSpec("ABS", 1, 1, "ABS(x)", "Absolute value.", "ABS(ADJUSTMENT)"),
    FunctionSpec("MOD", 2, 2, "MOD(x, divisor)", "Excel MOD: remainder with the sign of the divisor.",
                 "MOD(DAYS_PRESENT, 7)"),
    FunctionSpec("LOOKUP", 2, 3, "LOOKUP(table, key, default)",
                 "Value of the row whose key matches (case/spacing-insensitive: 'Highly Skilled' = HIGHLY_SKILLED) "
                 "in one of the group's lookup tables; falls back to `default`, then the table's default, "
                 "else the employee errors.", "LOOKUP(ALLOWANCE_PER_DAY, SKILL)"),
    FunctionSpec("ISBLANK", 1, 1, "ISBLANK(variable)",
                 "1 when a pay-profile field, employee field, monthly input or DAYS_PRESENT was not provided "
                 "(the variable itself then reads as 0 / empty text).", "IF(ISBLANK(PF_CAP), 1800, PF_CAP)"),
    FunctionSpec("ERROR", 1, 1, "ERROR(\"message\")",
                 "Stops this employee's calculation with the message (shown on the run as an Error line).",
                 "IF(ISBLANK(DAY_WAGE), ERROR(\"Pay profile has no daily_wage.\"), DAY_WAGE)"),
)}


class MathError(ValueError):
    pass


def _digits(value: Decimal) -> int:
    if value != value.to_integral_value():
        raise MathError("the number of decimals must be a whole number")
    digits = int(value)
    if not -10 <= digits <= 10:
        raise MathError("the number of decimals must be between -10 and 10")
    return digits


def _quantize(fn, value: Decimal, digits: int) -> Decimal:
    try:
        return fn(value, digits)
    except InvalidOperation:
        raise MathError("number too large to round") from None


def excel_round(value: Decimal, digits: Decimal = Decimal(0)) -> Decimal:
    return _quantize(xl_round, value, _digits(digits))


def excel_roundup(value: Decimal, digits: Decimal = Decimal(0)) -> Decimal:
    return _quantize(xl_roundup, value, _digits(digits))


def xl_rounddown(value: Decimal, digits: int = 0) -> Decimal:
    return _clean(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_DOWN)


def excel_rounddown(value: Decimal, digits: Decimal = Decimal(0)) -> Decimal:
    return _quantize(xl_rounddown, value, _digits(digits))


def _to_multiple(value: Decimal, multiple: Decimal, rounding) -> Decimal:
    if multiple <= 0:
        raise MathError("the multiple must be greater than 0")
    try:
        return _clean(value / multiple).to_integral_value(rounding=rounding) * multiple
    except InvalidOperation:
        raise MathError("number too large") from None


def ceiling(value: Decimal, multiple: Decimal = Decimal(1)) -> Decimal:
    return _to_multiple(value, multiple, ROUND_CEILING)


def floor(value: Decimal, multiple: Decimal = Decimal(1)) -> Decimal:
    return _to_multiple(value, multiple, ROUND_FLOOR)


def mod(value: Decimal, divisor: Decimal) -> Decimal:
    if divisor == 0:
        raise MathError("division by zero")
    return value - divisor * (value / divisor).to_integral_value(rounding=ROUND_FLOOR)


ROUNDERS = {"ROUND": excel_round, "ROUNDUP": excel_roundup, "ROUNDDOWN": excel_rounddown}
LINE_ROUNDERS = {"ROUND": xl_round, "ROUNDUP": xl_roundup, "ROUNDDOWN": xl_rounddown}
