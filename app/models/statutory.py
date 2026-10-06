"""Statutory configuration tables — versioned by effective_from/effective_to
so nothing about PF/ESI/PT/TDS is ever hardcoded in engine code. PT and TDS
seed data is explicitly flagged `is_verified=False`; see db/seed/pt_tds_seed.py
and the report handed back after this phase for the verification disclaimer.
"""
from datetime import date
from typing import Any, Optional

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin
from app.models.json_type import JSONType


class PFConfig(Base, TimestampMixin):
    __tablename__ = "pf_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    employee_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.12, nullable=False)
    employer_eps_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.0833, nullable=False)
    employer_epf_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.0367, nullable=False)
    wage_ceiling: Mapped[float] = mapped_column(Numeric(12, 2), default=15000, nullable=False)
    eps_wage_ceiling: Mapped[float] = mapped_column(Numeric(12, 2), default=15000, nullable=False)
    # Company policy toggle: whether PF is capped at wage_ceiling or charged on full PF_WAGES.
    apply_ceiling: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ESIConfig(Base, TimestampMixin):
    __tablename__ = "esi_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    employee_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.0075, nullable=False)
    employer_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.0325, nullable=False)
    wage_eligibility_threshold: Mapped[float] = mapped_column(Numeric(12, 2), default=21000, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class PTState(Base, TimestampMixin):
    __tablename__ = "pt_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    code: Mapped[str] = mapped_column(String(5), unique=True, nullable=False, index=True)

    slabs: Mapped[list["PTSlab"]] = relationship(back_populates="state")


class PTSlab(Base, TimestampMixin):
    __tablename__ = "pt_slabs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    state_id: Mapped[int] = mapped_column(ForeignKey("pt_states.id"), nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    income_min: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    income_max: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)  # null = no upper bound
    tax_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    periodicity: Mapped[str] = mapped_column(String(20), nullable=False)  # Monthly|HalfYearly
    # Seeded slabs are NOT verified against a live source — see notes below and
    # the report accompanying this phase. Confirm against the current state
    # Professional Tax Act before running live payroll.
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    state: Mapped["PTState"] = relationship(back_populates="slabs")


class TDSSlab(Base, TimestampMixin):
    __tablename__ = "tds_slabs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    regime: Mapped[str] = mapped_column(String(10), nullable=False)  # Old|New
    financial_year: Mapped[str] = mapped_column(String(10), nullable=False)  # e.g. "2025-26"
    income_from: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    income_to: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    rate: Mapped[float] = mapped_column(Numeric(6, 4), nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class TDSConfig(Base, TimestampMixin):
    __tablename__ = "tds_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    financial_year: Mapped[str] = mapped_column(String(10), nullable=False)
    standard_deduction: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    cess_rate: Mapped[float] = mapped_column(Numeric(6, 4), default=0.04, nullable=False)
    rebate_87a_income_limit: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    rebate_87a_max_amount: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class EmployeeTaxDeclaration(Base, TimestampMixin):
    __tablename__ = "employee_tax_declarations"
    __table_args__ = (UniqueConstraint("employee_id", "financial_year", name="uq_tax_decl_employee_fy"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False, index=True)
    financial_year: Mapped[str] = mapped_column(String(10), nullable=False)
    regime_choice: Mapped[str] = mapped_column(String(10), nullable=False, default="New")  # Old|New
    # e.g. {"section_80c": 50000, "section_80d": 15000, "hra_exemption": 120000}
    declared_investments: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    previous_employer_income: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
