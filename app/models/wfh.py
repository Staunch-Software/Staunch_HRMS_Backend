"""Work-from-home requests: same approval workflow as leave. An approved WFH
day counts as present in attendance processing with no punch needed."""
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, ForeignKey, Index, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.leave import HALF_FULL, _ApprovalFields


class WfhRequest(Base, _ApprovalFields, TimestampMixin):
    __tablename__ = "wfh_requests"
    __table_args__ = (Index("ix_wfh_requests_employee_dates", "employee_id", "from_date", "to_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    # FIRST_HALF / SECOND_HALF only for a single-day request.
    half: Mapped[str] = mapped_column(String(12), nullable=False, default=HALF_FULL)
    days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    employee = relationship("Employee", foreign_keys="WfhRequest.employee_id")
    manager = relationship("Employee", foreign_keys="WfhRequest.manager_id")
