"""Errors raised by the pay-group formula language. Positions are 1-based
character offsets into the formula text."""
from typing import Optional

from app.services.payroll_engine.pay_groups.base import PayGroupCalculationError


def _where(line: Optional[str], position: Optional[int], field: Optional[str]) -> str:
    parts = []
    if line:
        parts.append(f"Line {line}" + (f" {field}" if field and field != "formula" else ""))
    if position:
        parts.append(f"position {position}")
    return ", ".join(parts)


class FormulaError(ValueError):
    """Bad formula text or reference (found when the definition is validated)."""

    def __init__(self, message: str, position: Optional[int] = None, line: Optional[str] = None,
                 field: Optional[str] = None):
        self.message = message
        self.position = position
        self.line = line
        self.field = field
        super().__init__(self._text())

    def _text(self) -> str:
        where = _where(self.line, self.position, self.field)
        return f"{where}: {self.message}" if where else self.message

    def at_line(self, line: str, field: str = "formula") -> "FormulaError":
        if self.line is None:
            self.line, self.field = line, field
            self.args = (self._text(),)
        return self

    def to_dict(self) -> dict:
        return {"line": self.line, "field": self.field, "position": self.position, "message": self.message}


class FormulaRuntimeError(PayGroupCalculationError):
    """A formula failed while calculating one employee (division by zero,
    missing lookup row, ERROR(...)). The run records it as an Error line item."""

    def __init__(self, message: str, position: Optional[int] = None, line: Optional[str] = None,
                 field: Optional[str] = None, user_message: bool = False):
        self.message = message
        self.position = position
        self.line = line
        self.field = field
        self.user_message = user_message
        super().__init__(self._text())

    def _text(self) -> str:
        if self.user_message:
            return self.message
        where = _where(self.line, self.position, self.field)
        return f"{where}: {self.message}" if where else self.message

    def at_line(self, line: str, field: str = "formula") -> "FormulaRuntimeError":
        if self.line is None:
            self.line, self.field = line, field
            self.args = (self._text(),)
        return self
