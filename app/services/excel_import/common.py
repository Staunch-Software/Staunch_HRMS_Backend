"""Shared row-result shape and small parsing helpers used by both the
variable-inputs and final-sheet validators.
"""
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

import pandas as pd

# "Small rounding tolerance" for Gross - Deductions vs Net Pay on the
# final-sheet template — Indian payroll sheets commonly round each
# component to the nearest rupee, so a few rupees of drift across many
# components is expected and should not block a commit.
FINAL_SHEET_TOLERANCE = Decimal("1.00")


@dataclass
class RowResult:
    row_number: int
    data: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_response(self) -> dict:
        return {
            "row_number": self.row_number,
            "data": self.data,
            "errors": self.errors,
            "warnings": self.warnings,
        }


def clean_str(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def pay_period_str(value: Any) -> Optional[str]:
    """Normalizes a "Pay Period" cell to "YYYY-MM". Excel sometimes silently
    reformats a "2026-09"-looking text cell as an actual date, so this
    accepts both a plain string and a date/Timestamp value.
    """
    if value is None:
        return None
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.strftime("%Y-%m")
    return str(value).strip()


def to_number(
    value: Any, *, required: bool = False, default: Optional[float] = None, field_name: str = "value"
) -> tuple[Optional[float], Optional[str]]:
    """Parses a cell as a number. Returns (value, error_message) — error is
    None on success. Blank/None resolves to `default` (or an error if
    `required`).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            return None, f"{field_name} is required."
        return default, None
    try:
        return float(value), None
    except (TypeError, ValueError):
        return default, f"{field_name} must be numeric (got {value!r})."


def to_decimal(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def flag_duplicate_employee_codes(results: list[RowResult]) -> None:
    """Blocking error (not just a warning) — committing two rows for the
    same employee would violate the DB's per-employee-per-period unique
    constraint, so this must be fixed before "Confirm & Apply" is enabled.
    """
    rows_by_code: dict[str, list[int]] = {}
    for result in results:
        code = result.data.get("employee_code")
        if code:
            rows_by_code.setdefault(code, []).append(result.row_number)

    for result in results:
        code = result.data.get("employee_code")
        if not code:
            continue
        other_rows = [r for r in rows_by_code.get(code, []) if r != result.row_number]
        if other_rows:
            result.errors.append(
                f"Duplicate Employee Code '{code}' also appears in row(s) "
                f"{', '.join(str(r) for r in other_rows)}."
            )
