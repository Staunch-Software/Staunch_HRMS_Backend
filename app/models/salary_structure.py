from datetime import date
from typing import Optional

from sqlalchemy import Date, ForeignKey, Integer, Numeric
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class EmployeeSalaryStructureAssignment(Base, TimestampMixin):
    """Per-employee link to a salary component, versioned by effective_from/to.

    Editing NEVER mutates a row in place: it closes out the existing open row
    (sets effective_to) and inserts a new one, so a past payroll run stays
    reproducible against the structure as it existed on its own period_end.
    """

    __tablename__ = "employee_salary_structure_assignments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False, index=True)
    salary_component_id: Mapped[int] = mapped_column(
        ForeignKey("salary_components.id"), nullable=False, index=True
    )
    # Overrides the component's default `value` for this employee only (e.g. a
    # custom Basic Pay amount). Null = use the component's own stored value.
    value_override: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[Optional[date]] = mapped_column(Date, nullable=True)  # null = open-ended

    employee = relationship("Employee")
    salary_component = relationship("SalaryComponent")
