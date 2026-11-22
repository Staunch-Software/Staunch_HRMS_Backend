"""Leave types, leave and permission requests, and the leave ledger.

Balances are never stored: a balance is the sum of LeaveLedger.days for an
(employee, leave type, leave year). Credits are positive, debits negative.
"""
import datetime as dt
from datetime import date, datetime, time
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, _utcnow
from app.models.json_type import JSONType

CREDIT_YEARLY_UPFRONT = "YEARLY_UPFRONT"
CREDIT_MONTHLY_ACCRUAL = "MONTHLY_ACCRUAL"
# Days per month chosen by length of service (LeaveType.service_tiers).
CREDIT_MONTHLY_TIERED = "MONTHLY_TIERED"
CREDIT_MODES = (CREDIT_YEARLY_UPFRONT, CREDIT_MONTHLY_ACCRUAL, CREDIT_MONTHLY_TIERED)

HALF_FULL = "FULL"
HALF_FIRST = "FIRST_HALF"
HALF_SECOND = "SECOND_HALF"
HALVES = (HALF_FULL, HALF_FIRST, HALF_SECOND)

STATUS_PENDING_MANAGER = "PENDING_MANAGER"
STATUS_PENDING_HR = "PENDING_HR"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_CANCELLED = "CANCELLED"
PENDING_STATUSES = (STATUS_PENDING_MANAGER, STATUS_PENDING_HR)
ACTIVE_STATUSES = (STATUS_PENDING_MANAGER, STATUS_PENDING_HR, STATUS_APPROVED)
STATUSES = (STATUS_PENDING_MANAGER, STATUS_PENDING_HR, STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELLED)

ENTRY_CREDIT = "CREDIT"
ENTRY_CARRY_FORWARD = "CARRY_FORWARD"
ENTRY_LEAVE_DEBIT = "LEAVE_DEBIT"
ENTRY_LATE_DEBIT = "LATE_DEBIT"
ENTRY_ADJUSTMENT = "ADJUSTMENT"
ENTRY_REVERSAL = "REVERSAL"
# Unused monthly credit removed for a service tier without carry forward.
ENTRY_LAPSE = "LAPSE"
ENTRY_TYPES = (
    ENTRY_CREDIT, ENTRY_CARRY_FORWARD, ENTRY_LEAVE_DEBIT, ENTRY_LATE_DEBIT, ENTRY_ADJUSTMENT, ENTRY_REVERSAL,
    ENTRY_LAPSE,
)


class LeaveType(Base, TimestampMixin):
    __tablename__ = "leave_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    annual_entitlement: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False, default=Decimal("0"))
    credit_mode: Mapped[str] = mapped_column(String(20), nullable=False, default=CREDIT_YEARLY_UPFRONT)
    pro_rata_for_joiners: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    carry_forward: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # NULL = no cap on the carried-forward balance.
    carry_forward_limit: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2), nullable=True)
    is_paid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allow_half_day: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sandwich_rule: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    allowed_during_notice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allow_negative_balance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # MONTHLY_TIERED: [{min_service_months, days_per_month, carry_forward,
    # carry_forward_limit}]; service = completed months at the month start.
    service_tiers: Mapped[Optional[list]] = mapped_column(JSONType, nullable=True)
    # A joiner gets the joining month's credit only when joined on/before this day.
    joining_cutoff_day: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def tracks_balance(self) -> bool:
        """Unpaid leave (LOP) has no balance and writes no ledger rows."""
        return bool(self.is_paid)


class _ApprovalFields:
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    applied_by_user_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    recorded_by_hr: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Manager the request was routed to (the department manager, or the
    # reporting manager under the fallback); NULL: went straight to HR.
    manager_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL"), nullable=True
    )
    manager_action_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    manager_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    manager_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hr_action_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    hr_action_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    hr_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancelled_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class LeaveApplication(Base, _ApprovalFields, TimestampMixin):
    __tablename__ = "leave_applications"
    __table_args__ = (Index("ix_leave_applications_employee_dates", "employee_id", "from_date", "to_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    leave_type_id: Mapped[int] = mapped_column(ForeignKey("leave_types.id"), nullable=False)
    from_date: Mapped[date] = mapped_column(Date, nullable=False)
    to_date: Mapped[date] = mapped_column(Date, nullable=False)
    from_half: Mapped[str] = mapped_column(String(12), nullable=False, default=HALF_FULL)
    to_half: Mapped[str] = mapped_column(String(12), nullable=False, default=HALF_FULL)
    days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    employee = relationship("Employee", foreign_keys="LeaveApplication.employee_id")
    leave_type = relationship("LeaveType")
    manager = relationship("Employee", foreign_keys="LeaveApplication.manager_id")


class PermissionRequest(Base, _ApprovalFields, TimestampMixin):
    __tablename__ = "permission_requests"
    __table_args__ = (Index("ix_permission_requests_employee_date", "employee_id", "date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    from_time: Mapped[time] = mapped_column(Time, nullable=False)
    to_time: Mapped[time] = mapped_column(Time, nullable=False)
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Set when HR recorded or approved it beyond the monthly limits.
    override_limits: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    employee = relationship("Employee", foreign_keys="PermissionRequest.employee_id")
    manager = relationship("Employee", foreign_keys="PermissionRequest.manager_id")


class LeaveLedger(Base):
    __tablename__ = "leave_ledger"
    __table_args__ = (
        Index("ix_leave_ledger_balance", "employee_id", "leave_type_id", "leave_year"),
        Index("ix_leave_ledger_reference", "reference_type", "reference_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False)
    leave_type_id: Mapped[int] = mapped_column(ForeignKey("leave_types.id"), nullable=False)
    leave_year: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_type: Mapped[str] = mapped_column(String(20), nullable=False)
    days: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    # e.g. ("leave_application", "42"), ("yearly_credit", "2027"),
    # ("accrual", "2027-03"), ("attendance_month", "2026-11").
    reference_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    reference_id: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)

    leave_type = relationship("LeaveType")
