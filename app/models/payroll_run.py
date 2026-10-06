from datetime import date, datetime
from typing import Any, Optional

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.json_type import JSONType


class PayrollRun(Base, TimestampMixin):
    __tablename__ = "payroll_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pay_schedule_id: Mapped[int] = mapped_column(ForeignKey("pay_schedules.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    pay_date: Mapped[date] = mapped_column(Date, nullable=False)
    run_type: Mapped[str] = mapped_column(String(30), default="Regular", nullable=False)
    # Draft|Processing|Calculated|PendingApproval|Approved|Paid|Cancelled|Failed
    status: Mapped[str] = mapped_column(String(30), default="Draft", nullable=False)
    # InAppCalculation only in this phase; ExcelFinalUpload is Phase 3.
    source: Mapped[str] = mapped_column(String(30), default="InAppCalculation", nullable=False)

    total_gross: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    total_deductions: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    total_net: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    employee_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    calculated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    pay_schedule = relationship("PaySchedule")
    line_items: Mapped[list["PayrollRunLineItem"]] = relationship(
        back_populates="payroll_run", cascade="all, delete-orphan"
    )


class PayrollRunLineItem(Base, TimestampMixin):
    __tablename__ = "payroll_run_line_items"
    __table_args__ = (
        UniqueConstraint("payroll_run_id", "employee_id", name="uq_run_line_item_employee"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    payroll_run_id: Mapped[int] = mapped_column(ForeignKey("payroll_runs.id"), nullable=False, index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False, index=True)

    # Freezes employee_code/name/department/designation/bank details as of
    # calculation time, since bank details can change before disbursement.
    employee_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    gross_earnings: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total_deductions: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total_reimbursements: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    net_pay: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    pf_wages: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    esi_wages: Mapped[Optional[float]] = mapped_column(Numeric(14, 2), nullable=True)
    paid_days: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)
    lop_days: Mapped[Optional[float]] = mapped_column(Numeric(6, 2), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="Included", nullable=False)  # Included|Excluded|OnHold|Error
    # List of {component_code, component_name, component_type, calculation_method, formula_or_value, resolved_amount}
    computation_breakdown: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    payroll_run = relationship("PayrollRun", back_populates="line_items")
    employee = relationship("Employee")
