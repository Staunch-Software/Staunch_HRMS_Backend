from typing import Optional

from pydantic import BaseModel, field_validator


class SalaryComponentBase(BaseModel):
    name: str
    code: str
    component_type: str  # Earning|Deduction|Reimbursement
    status: bool = True
    description: Optional[str] = None
    payslip_order: Optional[int] = None

    earning_sub_type: Optional[str] = None
    deduction_sub_type: Optional[str] = None
    statutory_type: Optional[str] = None

    is_taxable: Optional[str] = None
    tax_section: Optional[str] = None

    calculation_method: Optional[str] = None
    value: Optional[str] = None

    affects_pf: Optional[bool] = None
    affects_esi: Optional[bool] = None
    is_prorated: Optional[bool] = None
    is_recurring: Optional[bool] = None
    rounding_rule: Optional[str] = None

    proof_required: Optional[bool] = None
    claim_limit_type: Optional[str] = None
    claim_limit_amount: Optional[float] = None

    @field_validator("code")
    @classmethod
    def uppercase_code(cls, v: str) -> str:
        return v.strip().upper()


class SalaryComponentCreate(SalaryComponentBase):
    pass


class SalaryComponentUpdate(SalaryComponentBase):
    pass


class SalaryComponentRead(SalaryComponentBase):
    id: int

    model_config = {"from_attributes": True}
