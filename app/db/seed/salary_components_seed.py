"""The 6 salary components ported verbatim from the frontend mock
(src/services/payrollApiService.js) so demo data survives the cutover to a
real backend.
"""
from sqlalchemy.orm import Session

from app.models.salary_component import SalaryComponent

SALARY_COMPONENTS_SEED = [
    dict(
        name="Basic Pay",
        code="BASIC",
        component_type="Earning",
        status=True,
        description="Core basic salary component.",
        payslip_order=1,
        earning_sub_type="Fixed Pay",
        is_taxable="Yes",
        tax_section=None,
        calculation_method="Fixed Amount",
        value=None,
        affects_pf=True,
        affects_esi=True,
        is_prorated=True,
        is_recurring=True,
    ),
    dict(
        name="House Rent Allowance",
        code="HRA",
        component_type="Earning",
        status=True,
        description="Allowance for house rent.",
        payslip_order=2,
        earning_sub_type="Allowance",
        is_taxable="Partially Exempt",
        tax_section="Sec 10(13A)",
        calculation_method="Percentage/Formula",
        value="0.4 * BASIC",
        affects_pf=False,
        affects_esi=True,
        is_prorated=True,
        is_recurring=True,
    ),
    dict(
        name="Provident Fund (Employee)",
        code="PF_EE",
        component_type="Deduction",
        status=True,
        description="Employee contribution to EPF.",
        payslip_order=101,
        deduction_sub_type="Statutory",
        calculation_method="Percentage/Formula",
        value="0.12 * PF_WAGES",
        statutory_type="PF",
        rounding_rule="Nearest Rupee",
    ),
    dict(
        name="Professional Tax",
        code="PT",
        component_type="Deduction",
        status=True,
        description="State Professional Tax.",
        payslip_order=102,
        deduction_sub_type="Statutory",
        calculation_method="Slab Based",
        value=None,
        statutory_type="PT",
        rounding_rule="Upper Rupee",
    ),
    dict(
        name="Canteen Deduction",
        code="CANTEEN",
        component_type="Deduction",
        status=True,
        description="Monthly deduction for canteen usage.",
        payslip_order=150,
        deduction_sub_type="Voluntary",
        calculation_method="Fixed Amount",
        value="500",
        statutory_type=None,
        rounding_rule="None",
    ),
    dict(
        name="Travel Reimbursement",
        code="TRAVEL_REIM",
        component_type="Reimbursement",
        status=True,
        description="Reimbursement for official travel expenses.",
        payslip_order=201,
        is_taxable="No",
        tax_section="Rule 2BB",
        proof_required=True,
        claim_limit_type="Monthly",
        claim_limit_amount=5000,
    ),
]


def seed_salary_components(db: Session) -> None:
    for data in SALARY_COMPONENTS_SEED:
        exists = db.query(SalaryComponent).filter(SalaryComponent.code == data["code"]).first()
        if exists:
            continue
        db.add(SalaryComponent(**data))
    db.commit()
