from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field

from app.models.leave import HALF_FULL
from app.schemas.leave import ApprovalsInbox, LeaveDayRead, RequestBase


class WfhApplyRequest(BaseModel):
    from_date: date
    to_date: date
    half: str = HALF_FULL
    reason: Optional[str] = Field(default=None, max_length=1000)


class WfhRecordRequest(WfhApplyRequest):
    employee_id: int
    comment: Optional[str] = Field(default=None, max_length=1000)


class WfhRequestRead(RequestBase):
    from_date: date
    to_date: date
    half: str
    days: Decimal


class WfhPreview(BaseModel):
    days: Decimal
    breakdown: list[LeaveDayRead]
    errors: list[str] = []


class ApprovalsInboxWithWfh(ApprovalsInbox):
    wfh: list[WfhRequestRead] = []


class WfhRules(BaseModel):
    min_notice_days: int
    has_manager: bool
    manager_name: Optional[str] = None
    chain: str = "MANAGER_HR"
