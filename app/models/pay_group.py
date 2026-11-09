from datetime import date
from typing import Any, Optional

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.json_type import JSONType


class PayGroup(Base, TimestampMixin):
    """One location/client-contract payroll model. `calc_model` picks the
    strategy in services/payroll_engine/pay_groups; every number the strategy
    uses (rates, caps, thresholds, standard days, skill tables) is in `params`
    so HR can revise minimum wages as data."""

    __tablename__ = "pay_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    calc_model: Mapped[str] = mapped_column(String(50), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class EmployeePayProfile(Base, TimestampMixin):
    """Per-employee pay-group membership and the per-employee figures the
    group's strategy needs. Effective-dated like salary-structure
    assignments: a change closes the open profile (effective_to) and inserts
    a new one, so past runs stay reproducible."""

    __tablename__ = "employee_pay_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False, index=True)
    pay_group_id: Mapped[int] = mapped_column(ForeignKey("pay_groups.id"), nullable=False, index=True)

    monthly_salary: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    daily_wage: Mapped[Optional[float]] = mapped_column(Numeric(12, 4), nullable=True)
    skill_level: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Null = fall back to Employee.pf_applicable / esi_applicable, then the group default.
    pf_applicable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    esi_applicable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    pf_cap: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    fixed_monthly_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    # Strategy-specific per-employee values, e.g. {"base_day_rate": 582,
    # "supervisor_allowance": 2000} for DAILY_FLAT_RATE.
    extra: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)

    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # null = open-ended

    employee = relationship("Employee")
    pay_group = relationship("PayGroup")
