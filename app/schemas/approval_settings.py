from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel

Chain = Literal["MANAGER_HR", "HR_ONLY", "MANAGER_ONLY"]
Fallback = Literal["HR", "REPORTING_MANAGER"]


class ApprovalSettingsBase(BaseModel):
    leave_chain: Chain = "MANAGER_HR"
    permission_chain: Chain = "MANAGER_HR"
    wfh_chain: Chain = "MANAGER_HR"
    regularization_chain: Chain = "MANAGER_HR"
    no_manager_fallback: Fallback = "HR"
    payroll_maker_checker: bool = False


class ApprovalSettingsUpdate(ApprovalSettingsBase):
    pass


class ApprovalSettingsRead(ApprovalSettingsBase):
    updated_at: Optional[datetime] = None

    model_config = {"from_attributes": True}
