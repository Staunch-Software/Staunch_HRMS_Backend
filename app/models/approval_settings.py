"""Approval workflow settings (single row): the approval chain per request
type, where requests go when there is no eligible department manager, and
payroll-run maker-checker."""
from typing import Optional

from sqlalchemy import Boolean, ForeignKey, Integer, String, false, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

CHAIN_MANAGER_HR = "MANAGER_HR"
CHAIN_HR_ONLY = "HR_ONLY"
CHAIN_MANAGER_ONLY = "MANAGER_ONLY"
CHAINS = (CHAIN_MANAGER_HR, CHAIN_HR_ONLY, CHAIN_MANAGER_ONLY)

FALLBACK_HR = "HR"
FALLBACK_REPORTING_MANAGER = "REPORTING_MANAGER"
FALLBACKS = (FALLBACK_HR, FALLBACK_REPORTING_MANAGER)


class ApprovalSettings(Base, TimestampMixin):
    __tablename__ = "approval_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    leave_chain: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CHAIN_MANAGER_HR, server_default=text(f"'{CHAIN_MANAGER_HR}'"))
    permission_chain: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CHAIN_MANAGER_HR, server_default=text(f"'{CHAIN_MANAGER_HR}'"))
    wfh_chain: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CHAIN_MANAGER_HR, server_default=text(f"'{CHAIN_MANAGER_HR}'"))
    regularization_chain: Mapped[str] = mapped_column(
        String(20), nullable=False, default=CHAIN_MANAGER_HR, server_default=text(f"'{CHAIN_MANAGER_HR}'"))
    no_manager_fallback: Mapped[str] = mapped_column(
        String(20), nullable=False, default=FALLBACK_HR, server_default=text(f"'{FALLBACK_HR}'"))
    payroll_maker_checker: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false())
    updated_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
