from datetime import date
from typing import Optional

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Employee(Base, TimestampMixin):
    __tablename__ = "employees"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_code: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    middle_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    last_name: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    work_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    personal_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    department_id: Mapped[Optional[int]] = mapped_column(ForeignKey("departments.id"), nullable=True)
    designation_id: Mapped[Optional[int]] = mapped_column(ForeignKey("designations.id"), nullable=True)
    work_location_id: Mapped[Optional[int]] = mapped_column(ForeignKey("work_locations.id"), nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="Active", nullable=False)
    profile_picture_url: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)

    date_of_joining: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    date_of_leaving: Mapped[Optional[date]] = mapped_column(Date, nullable=True)

    basic_salary: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)

    mobile_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    gender: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # --- Personal ---
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    marital_status: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    blood_group: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)
    father_spouse_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    relation: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    emergency_contact_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    emergency_contact_number: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    # Free-text address as it appears in the master file; the structured
    # fields below are what the Add/Edit form collects.
    permanent_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    address_line1: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    address_line2: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    city: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    state: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    country: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    postal_code: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # TODO: encrypt at rest before production (same as account_number).
    aadhaar_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # TODO: encrypt at rest before production (same as account_number).
    pan_number: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    # --- Employment / statutory ---
    employment_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    pf_applicable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    uan_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    esi_applicable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    esi_number: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    insurance_applicable: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    geographic_location: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    client: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    client_branch: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    remarks: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- Education ---
    highest_qualification: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    certifications: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    year_of_passing: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    university: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)

    # --- Apprenticeship / contract ---
    apprenticeship_type: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    enrollment_id: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    contract_no: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)
    contract_start_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    contract_end_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    stipend_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)

    department = relationship("Department")
    designation = relationship("Designation")
    work_location = relationship("WorkLocation")
    bank_accounts: Mapped[list["EmployeeBankAccount"]] = relationship(
        back_populates="employee", cascade="all, delete-orphan"
    )


class EmployeeBankAccount(Base, TimestampMixin):
    __tablename__ = "employee_bank_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), nullable=False, index=True)

    bank_name: Mapped[str] = mapped_column(String(150), nullable=False)
    account_holder_name: Mapped[str] = mapped_column(String(150), nullable=False)
    # TODO: encrypt at rest before production, e.g. via pgcrypto or app-level
    # Fernet — deferred to keep Phase 1 scope contained. Plain string for now.
    account_number: Mapped[str] = mapped_column(String(40), nullable=False)
    # Validated in Pydantic schemas against ^[A-Z]{4}0[A-Z0-9]{6}$ (matches the
    # existing frontend regex in PaymentMethod.js) rather than a DB CHECK
    # constraint, so it works identically on SQLite (dev) and Postgres (prod).
    ifsc_code: Mapped[str] = mapped_column(String(11), nullable=False)
    branch_name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    account_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # Savings/Current
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    employee: Mapped["Employee"] = relationship(back_populates="bank_accounts")
