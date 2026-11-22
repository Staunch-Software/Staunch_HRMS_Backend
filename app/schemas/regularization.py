import datetime as dt
from datetime import time
from typing import Optional

from pydantic import BaseModel, Field

from app.schemas.leave import RequestBase
from app.schemas.wfh import ApprovalsInboxWithWfh


class RegularizationApplyRequest(BaseModel):
    date: dt.date
    punch_in: Optional[time] = None
    punch_out: Optional[time] = None
    reason_code: str
    reason: Optional[str] = Field(default=None, max_length=1000)


class RegularizationRead(RequestBase):
    date: dt.date
    punch_in: Optional[time] = None
    punch_out: Optional[time] = None
    reason_code: str
    reason_label: str
    applied_sides: Optional[str] = None
    override_id: Optional[int] = None


class ReasonOption(BaseModel):
    code: str
    label: str


class RegularizationRules(BaseModel):
    max_back_days: int
    monthly_cap: int
    used_this_month: int
    earliest_date: dt.date
    reasons: list[ReasonOption]
    has_manager: bool
    manager_name: Optional[str] = None
    chain: str


class ApprovalsInboxAll(ApprovalsInboxWithWfh):
    regularizations: list[RegularizationRead] = []
