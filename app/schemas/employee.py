import re
from datetime import date
from typing import Optional

from pydantic import BaseModel, field_validator

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
    basic_salary: Optional[float] = None
    mobile_number: Optional[str] = None
    gender: Optional[str] = None


class EmployeeCreate(EmployeeBase):
    pass


class EmployeeUpdate(EmployeeBase):
    pass


class EmployeeStatusUpdate(BaseModel):
    status: str


class EmployeeRead(EmployeeBase):
    id: int

    model_config = {"from_attributes": True}


class EmployeeBankAccountBase(BaseModel):
    bank_name: str
    account_holder_name: str
    account_number: str
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
    def validate_account_number(cls, v: str) -> str:
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
