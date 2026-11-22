from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class PFConfigBase(BaseModel):
    effective_from: date
    effective_to: Optional[date] = None
    employee_rate: float = 0.12
    employer_eps_rate: float = 0.0833
    employer_epf_rate: float = 0.0367
    wage_ceiling: float = 15000
    eps_wage_ceiling: float = 15000
    apply_ceiling: bool = True
    is_active: bool = True


class PFConfigCreate(PFConfigBase):
    pass


class PFConfigRead(PFConfigBase):
    id: int

    model_config = {"from_attributes": True}


class ESIConfigBase(BaseModel):
    effective_from: date
    effective_to: Optional[date] = None
    employee_rate: float = 0.0075
    employer_rate: float = 0.0325
    wage_eligibility_threshold: float = 21000
    is_active: bool = True


class ESIConfigCreate(ESIConfigBase):
    pass


class ESIConfigRead(ESIConfigBase):
    id: int

    model_config = {"from_attributes": True}


class PTStateBase(BaseModel):
    name: str
    code: str


class PTStateCreate(PTStateBase):
    pass


class PTStateRead(PTStateBase):
    id: int

    model_config = {"from_attributes": True}


class PTSlabBase(BaseModel):
    state_id: int
    effective_from: date
    effective_to: Optional[date] = None
    income_min: float
    income_max: Optional[float] = None
    tax_amount: float
    periodicity: str = "Monthly"  # Monthly|HalfYearly
    is_verified: bool = False
    notes: Optional[str] = None


class PTSlabCreate(PTSlabBase):
    pass


class PTSlabRead(PTSlabBase):
    id: int

    model_config = {"from_attributes": True}


class TDSSlabBase(BaseModel):
    regime: str  # Old|New
    financial_year: str
    income_from: float
    income_to: Optional[float] = None
    rate: float
    is_verified: bool = False
    notes: Optional[str] = None


class TDSSlabCreate(TDSSlabBase):
    pass


class TDSSlabRead(TDSSlabBase):
    id: int

    model_config = {"from_attributes": True}


class SurchargeBand(BaseModel):
    above: float
    rate: float


class TDSConfigBase(BaseModel):
    financial_year: str
    regime: Literal["New", "Old"] = "New"
    standard_deduction: float
    cess_rate: float = 0.04
    rebate_87a_income_limit: float
    rebate_87a_max_amount: float
    surcharge_bands: list[SurchargeBand] = []
    is_active: bool = True
    is_verified: bool = False
    notes: Optional[str] = None


class TDSConfigCreate(TDSConfigBase):
    pass


class TDSConfigRead(TDSConfigBase):
    id: int

    model_config = {"from_attributes": True}


class EmployeeTaxDeclarationBase(BaseModel):
    financial_year: str
    regime_choice: str = "New"
    declared_investments: dict[str, Any] = {}
    previous_employer_income: Optional[float] = None
    previous_employer_tds: Optional[float] = None
    old_regime_deductions: Optional[float] = None


class EmployeeTaxDeclarationCreate(EmployeeTaxDeclarationBase):
    pass


class EmployeeTaxDeclarationRead(EmployeeTaxDeclarationBase):
    id: int
    employee_id: int

    model_config = {"from_attributes": True}


class TaxDeclarationUpdate(BaseModel):
    regime_choice: Literal["New", "Old"] = "New"
    old_regime_deductions: Optional[float] = Field(default=None, ge=0)
    previous_employer_income: Optional[float] = Field(default=None, ge=0)
    previous_employer_tds: Optional[float] = Field(default=None, ge=0)


class TaxDeclarationForYear(TaxDeclarationUpdate):
    """One financial year's declaration; id is null when none is saved (New regime applies)."""

    id: Optional[int] = None
    employee_id: int
    financial_year: str


class AnnualTaxBreakdown(BaseModel):
    taxable_income: float
    tax: float
    rebate: float
    marginal_relief: float
    surcharge: float
    surcharge_marginal_relief: float
    cess: float
    total: float


class TDSPreview(BaseModel):
    employee_id: int
    period_end: date
    financial_year: str
    regime: str
    has_declaration: bool
    gross_source: str  # query | latest_run | none
    current_month_gross: float
    months_remaining: int
    ytd_gross: float
    ytd_tds: float
    previous_employer_income: float
    previous_employer_tds: float
    annual_projection: float
    standard_deduction: float
    old_regime_deductions: float
    taxable_income: float
    annual_tax: float
    remaining_tax: float
    monthly_tds: float
    tax_breakdown: Optional[AnnualTaxBreakdown] = None
    warning: Optional[str] = None
