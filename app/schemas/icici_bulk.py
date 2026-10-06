from typing import Optional

from pydantic import BaseModel


class ICICIConfigStatus(BaseModel):
    configured: bool
    missing_fields: list[str]
    encryption_enabled: bool
    base_url: str


class ICICISubmitRequest(BaseModel):
    # Omit on the first call to trigger the OTP SMS; call again with the
    # OTP the user read off their phone to actually submit the file.
    otp: Optional[str] = None
    file_description: Optional[str] = None


class ICICISubmitResponse(BaseModel):
    otp_requested: bool
    unique_id: Optional[str] = None
    file_sequence_num: Optional[str] = None
    message: Optional[str] = None
    requires_manual_approval: Optional[bool] = None


class ICICITransactionStatus(BaseModel):
    transaction_type: Optional[str] = None
    network_id: Optional[str] = None
    credit_account_number: Optional[str] = None
    debit_account_number: Optional[str] = None
    ifsc_code: Optional[str] = None
    currency: Optional[str] = None
    total_amount: Optional[str] = None
    host_reference_number: Optional[str] = None
    host_response_code: Optional[str] = None
    host_response_message: Optional[str] = None
    transaction_remarks: Optional[str] = None
    transaction_status: Optional[str] = None
    raw: Optional[str] = None
    parse_error: Optional[str] = None


class ICICIStatusResponse(BaseModel):
    file_sequence_num: Optional[str] = None
    file_status_code: Optional[str] = None
    file_status_description: Optional[str] = None
    file_status_outcome: Optional[str] = None
    transactions: list[ICICITransactionStatus] = []
