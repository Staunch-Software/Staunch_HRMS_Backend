from datetime import date
from typing import Any, Optional

from pydantic import BaseModel


class PayScheduleBase(BaseModel):
    schedule_name: str
    pay_frequency: str  # Monthly|Semi-Monthly|Bi-Weekly|Weekly
    payment_day_rule: dict[str, Any]
    holiday_adjustment: str  # Before|After
    processing_day_offset: int = 0
    is_active: bool = True
    first_effective_pay_date: Optional[date] = None
    description: Optional[str] = None
    applies_to: Optional[dict[str, Any]] = None


class PayScheduleCreate(PayScheduleBase):
    pass


class PayScheduleUpdate(PayScheduleBase):
    pass


class PayScheduleRead(PayScheduleBase):
    id: int

    model_config = {"from_attributes": True}
