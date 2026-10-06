from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DisbursementBatchCreate(BaseModel):
    format_key: str = "generic_neft_xlsx_v1"


class DisbursementBatchRead(BaseModel):
    id: int
    payroll_run_id: int
    format_key: str
    file_name: str
    status: str
    total_amount: float
    total_employee_count: int
    generated_at: datetime
    generated_by: Optional[str] = None
    version: int
    checksum: str
    download_url: str
    icici_file_sequence_num: Optional[str] = None
    icici_submitted_at: Optional[datetime] = None
    icici_message: Optional[str] = None
    icici_otp_pending: bool = False

    model_config = {"from_attributes": True}


class DisbursementDataRow(BaseModel):
    employee_id: int
    employee_code: str
    name: str
    bank_name: Optional[str] = None
    account_number: Optional[str] = None
    ifsc_code: Optional[str] = None
    net_pay: float
