"""Monthly attendance processing results (app.services.attendance.monthly).

One row per (employee, month): the resolved days, the month totals and the
LOP that "send to payroll" writes into the period's VariablePayInput.
status PROCESSED -> SENT; re-processing a SENT row whose figures changed puts
it back to PROCESSED (sent_at stays set, so the UI shows "changed since sent").
"""
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.json_type import JSONType

MONTH_STATUS_PROCESSED = "PROCESSED"
MONTH_STATUS_SENT = "SENT"


class AttendanceMonthResult(Base, TimestampMixin):
    __tablename__ = "attendance_month_results"
    __table_args__ = (UniqueConstraint("employee_id", "month", name="uq_attendance_month_result"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False, index=True)  # YYYY-MM
    pay_group_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("pay_groups.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=MONTH_STATUS_PROCESSED)
    lop_days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal("0"))
    present_days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal("0"))
    paid_leave_days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal("0"))
    totals: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    days: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    warnings: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    processed_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    # Figures as last sent (lop/present/paid leave/debits), to tell whether a
    # re-process changed anything.
    sent_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONType, nullable=True)

    employee = relationship("Employee")
    pay_group = relationship("PayGroup")
