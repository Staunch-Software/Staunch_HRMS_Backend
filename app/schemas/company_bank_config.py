from pydantic import BaseModel


class CompanyBankConfigBase(BaseModel):
    company_code: str
    debit_account_number: str
    debit_account_bank_name: str
    is_active: bool = True


class CompanyBankConfigUpdate(CompanyBankConfigBase):
    pass


class CompanyBankConfigRead(CompanyBankConfigBase):
    id: int

    model_config = {"from_attributes": True}
