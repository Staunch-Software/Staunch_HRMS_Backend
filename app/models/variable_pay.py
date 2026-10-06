from datetime import date
from typing import Any, Optional

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.json_type import JSONType


class VariablePayInput(Base, TimestampMixin):
    """Manual per-period variable input (attendance/LOP/OT/ad-hoc pay), one
    row per employee per period. Excel bulk upload is Phase 3 — `source` can
    only be "Manual" in this phase.
    """

    __tablename__ = "variable_pay_inputs"
    __table_args__ = (
        UniqueConstraint("employee_id", "period_start", "period_end", name="uq_variable_input_employee_period"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False, index=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="Manual", nullable=False)

    present_days: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)
    lop_days: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    paid_leave_days: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    overtime_hours: Mapped[float] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    overtime_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)

    # Each item: {"component_code": str, "amount": number, "remarks": str}
    ad_hoc_earnings: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    ad_hoc_deductions: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)

    remarks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Draft|Locked — a run that has consumed this row locks it so it can no
    # longer be edited/replaced (past runs must stay reproducible).
    status: Mapped[str] = mapped_column(String(20), default="Draft", nullable=False)
    # Ties a row back to the Excel upload batch that created it (Phase 3).
    # Null for manually-entered rows (source="Manual").
    import_batch_id: Mapped[Optional[str]] = mapped_column(String(36), nullable=True)

    employee = relationship("Employee")
