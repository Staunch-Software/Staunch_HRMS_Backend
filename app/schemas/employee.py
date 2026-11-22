import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, computed_field, field_serializer, field_validator

from app.core.crypto import is_masked, last_digits, mask_aadhaar, mask_pan, mask_tail

IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


class EmployeeBase(BaseModel):
    employee_code: str
    first_name: str
    last_name: Optional[str] = None
    work_email: Optional[str] = None
    personal_email: Optional[str] = None
    department_id: Optional[int] = None
    designation_id: Optional[int] = None
    work_location_id: Optional[int] = None
    status: str = "Active"
    profile_picture_url: Optional[str] = None
    date_of_joining: Optional[date] = None
    date_of_leaving: Optional[date] = None
    notice_start_date: Optional[date] = None
    reporting_manager_id: Optional[int] = None
    basic_salary: Optional[float] = None
    mobile_number: Optional[str] = None
    gender: Optional[str] = None

    middle_name: Optional[str] = None
    date_of_birth: Optional[date] = None
    marital_status: Optional[str] = None
    blood_group: Optional[str] = None
    father_spouse_name: Optional[str] = None
    relation: Optional[str] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_number: Optional[str] = None
    permanent_address: Optional[str] = None
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None
    postal_code: Optional[str] = None
    aadhaar_number: Optional[str] = None
    pan_number: Optional[str] = None

    employment_type: Optional[str] = None
    pf_applicable: Optional[bool] = None
    uan_number: Optional[str] = None
    esi_applicable: Optional[bool] = None
    esi_number: Optional[str] = None
    insurance_applicable: Optional[bool] = None
    geographic_location: Optional[str] = None
    client: Optional[str] = None
    client_branch: Optional[str] = None
    remarks: Optional[str] = None

    highest_qualification: Optional[str] = None
    certifications: Optional[str] = None
    year_of_passing: Optional[str] = None
    university: Optional[str] = None

    apprenticeship_type: Optional[str] = None
    enrollment_id: Optional[str] = None
    contract_no: Optional[str] = None
    contract_start_date: Optional[date] = None
    contract_end_date: Optional[date] = None
    stipend_amount: Optional[float] = None


class EmployeeCreate(EmployeeBase):
    pass


class EmployeeUpdate(EmployeeBase):
    pass


class EmployeeStatusUpdate(BaseModel):
    status: str


class EmployeeRead(EmployeeBase):
    id: int
    reporting_manager_name: Optional[str] = None

    model_config = {"from_attributes": True}

    @field_serializer("aadhaar_number")
    def _mask_aadhaar(self, v: Optional[str]) -> Optional[str]:
        return mask_aadhaar(v)

    @field_serializer("pan_number")
    def _mask_pan(self, v: Optional[str]) -> Optional[str]:
        return mask_pan(v)


class EmployeeSensitiveRead(BaseModel):
    employee_id: int
    aadhaar_number: Optional[str] = None
    pan_number: Optional[str] = None
    account_number: Optional[str] = None


class EmployeeBankAccountBase(BaseModel):
    bank_name: str
    account_holder_name: str
    # On update, None or a masked value keeps the stored number.
    account_number: Optional[str] = None
    ifsc_code: str
    branch_name: Optional[str] = None
    account_type: Optional[str] = None  # Savings/Current
    is_primary: bool = True
    is_active: bool = True

    @field_validator("ifsc_code")
    @classmethod
    def validate_ifsc(cls, v: str) -> str:
        v = v.strip().upper()
        if not IFSC_RE.match(v):
            raise ValueError("IFSC Code format seems incorrect.")
        return v

    @field_validator("account_number")
    @classmethod
    def validate_account_number(cls, v: Optional[str]) -> Optional[str]:
        if v is None or is_masked(v):
            return v
        if not v or not v.isdigit():
            raise ValueError("Account Number should only contain digits.")
        return v


class EmployeeBankAccountCreate(EmployeeBankAccountBase):
    pass


class EmployeeBankAccountUpdate(EmployeeBankAccountBase):
    pass


class EmployeeBankAccountRead(EmployeeBankAccountBase):
    id: int
    employee_id: int

    model_config = {"from_attributes": True}

    @computed_field
    @property
    def account_number_last4(self) -> Optional[str]:
        return last_digits(self.account_number)

    @field_serializer("account_number")
    def _mask_account_number(self, v: Optional[str]) -> Optional[str]:
        return mask_tail(v)
