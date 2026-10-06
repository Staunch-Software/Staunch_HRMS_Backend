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

    designations: Mapped[list["Designation"]] = relationship(back_populates="department")


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
    state_code: Mapped[str] = mapped_column(String(5), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="Active", nullable=False)
