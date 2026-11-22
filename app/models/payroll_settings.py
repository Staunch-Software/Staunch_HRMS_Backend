"""Singleton payroll settings: rounding rules and payslip settings (with the
company profile printed on payslips). One row each; services fall back to
the defaults below when the row does not exist yet."""
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

NET_ROUNDINGS = ("NONE", "ROUND", "ROUNDUP", "ROUNDDOWN")
LINE_ROUNDINGS = ("NONE", "ROUND", "ROUNDUP", "ROUNDDOWN")
TDS_NEAREST_RUPEE = "NEAREST_RUPEE"
TDS_NEAREST_10 = "NEAREST_10"
TDS_ROUNDINGS = (TDS_NEAREST_RUPEE, TDS_NEAREST_10)
DAY_STEPS = ("0.5", "0.25", "NONE")

PAYSLIP_TEMPLATES = ("CLASSIC", "MODERN", "COMPACT")
PDF_PASSWORDS = ("NONE", "PAN", "DOB")
RELEASE_APPROVED = "APPROVED"
RELEASE_PAID = "PAID"
RELEASE_ON = (RELEASE_APPROVED, RELEASE_PAID)

ROUNDING_DEFAULTS = {
    "default_net_rounding": "ROUND",
    "default_line_rounding": "ROUND",
    "default_line_decimals": 0,
    "tds_rounding": TDS_NEAREST_RUPEE,
    "attendance_day_step": "0.5",
}

PAYSLIP_DEFAULTS = {
    "company_name": "Staunch Technologies Pvt Ltd",
    "company_address": None,
    "company_cin": None,
    "company_gstin": None,
    "template": "CLASSIC",
    "show_pan": True,
    "show_uan": True,
    "show_bank": True,
    "show_attendance": True,
    "show_leave_balance": True,
    "show_ytd": True,
    "show_employer_contrib": False,
    "footer_note": "This is a computer-generated payslip and does not require a signature.",
    "pdf_password": "NONE",
    "release_on": RELEASE_APPROVED,
}


class PayrollRoundingSettings(Base, TimestampMixin):
    __tablename__ = "payroll_rounding_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Net-pay rounding put into new pay groups' definitions.
    default_net_rounding: Mapped[str] = mapped_column(String(20), nullable=False, default="ROUND")
    # Rounding and decimals for new formula lines in the pay group builder.
    default_line_rounding: Mapped[str] = mapped_column(String(20), nullable=False, default="ROUND")
    default_line_decimals: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Monthly TDS: whole rupee, or nearest Rs 10 (section 288B).
    tds_rounding: Mapped[str] = mapped_column(String(20), nullable=False, default=TDS_NEAREST_RUPEE)
    # Step for the month's LOP / present / paid-leave day totals.
    attendance_day_step: Mapped[str] = mapped_column(String(10), nullable=False, default="0.5")
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)


class PayslipSettings(Base, TimestampMixin):
    __tablename__ = "payslip_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, default="Staunch Technologies Pvt Ltd")
    company_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    company_cin: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    company_gstin: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    # Uploaded logo; NULL = the bundled default (services/payslip/assets).
    logo: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    logo_mime: Mapped[Optional[str]] = mapped_column(String(50), nullable=True)

    template: Mapped[str] = mapped_column(String(20), nullable=False, default="CLASSIC")
    show_pan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_uan: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_bank: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_attendance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_leave_balance: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_ytd: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    show_employer_contrib: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    footer_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # NONE | PAN (upper case) | DOB (DDMMYYYY)
    pdf_password: Mapped[str] = mapped_column(String(10), nullable=False, default="NONE")
    # Employees see a run's payslips once it is APPROVED (or Paid) / only once PAID.
    release_on: Mapped[str] = mapped_column(String(10), nullable=False, default=RELEASE_APPROVED)
    updated_by: Mapped[Optional[int]] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True)
