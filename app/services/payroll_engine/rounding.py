"""Applies a salary component's `rounding_rule` to a Decimal amount.
Matches the option strings from SalaryComponentForm.js exactly:
"None" | "Nearest Rupee" | "Upper Rupee" | "Lower Rupee".
"""
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal


def apply_rounding(amount: Decimal, rounding_rule: str | None) -> Decimal:
    if not rounding_rule or rounding_rule == "None":
        return amount
    if rounding_rule == "Nearest Rupee":
        return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if rounding_rule == "Upper Rupee":
        return amount.quantize(Decimal("1"), rounding=ROUND_CEILING)
    if rounding_rule == "Lower Rupee":
        return amount.quantize(Decimal("1"), rounding=ROUND_FLOOR)
    # Unknown rule: don't crash the run over a config typo — leave unrounded.
    return amount
