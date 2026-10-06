from typing import Optional

from sqlalchemy import Boolean, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SalaryComponent(Base, TimestampMixin):
    """Single flat table mirroring SalaryComponentForm.js exactly. Nullable
    columns hold type-specific fields (earning-only, deduction-only,
    reimbursement-only) since Earning/Deduction/Reimbursement share one table.
    """

    __tablename__ = "salary_components"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False, index=True)  # stored uppercase
    component_type: Mapped[str] = mapped_column(String(20), nullable=False)  # Earning|Deduction|Reimbursement
    status: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    payslip_order: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    earning_sub_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    deduction_sub_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    statutory_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)

    is_taxable: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)  # Yes/No/Partially Exempt/Conditional
    tax_section: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    calculation_method: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    # Holds either a number-as-string or a formula string like "0.4 * BASIC"
    value: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    affects_pf: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    affects_esi: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_prorated: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    is_recurring: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    rounding_rule: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)

    proof_required: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    claim_limit_type: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    claim_limit_amount: Mapped[Optional[float]] = mapped_column(Numeric(12, 2), nullable=True)
