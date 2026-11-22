from typing import Optional

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Department(Base, TimestampMixin):
    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Active", nullable=False)
    # Approves leave, permission and WFH requests of the department's employees.
    manager_employee_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("employees.id", ondelete="SET NULL", use_alter=True, name="fk_departments_manager_employee_id"),
        nullable=True, index=True,
    )

    designations: Mapped[list["Designation"]] = relationship(back_populates="department")
    manager = relationship("Employee", foreign_keys=[manager_employee_id])


class Designation(Base, TimestampMixin):
    __tablename__ = "designations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    department_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("departments.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), default="Active", nullable=False)

    department: Mapped[Optional["Department"]] = relationship(back_populates="designations")


class WorkLocation(Base, TimestampMixin):
    __tablename__ = "work_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # Two-letter Indian state code (e.g. TN, MH, KA, DL) — drives Phase 2's
    # per-state Professional Tax slab lookup, so keep this accurate.
    # Nullable: a location created by the master import whose state can't be
    # inferred is left blank (PT is then skipped with a warning) rather than
    # guessed.
    state_code: Mapped[Optional[str]] = mapped_column(String(5), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Active", nullable=False)
