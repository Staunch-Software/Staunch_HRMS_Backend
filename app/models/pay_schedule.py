from datetime import date
from typing import Any, Optional

from sqlalchemy import Boolean, Date, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.json_type import JSONType


class PaySchedule(Base, TimestampMixin):
    __tablename__ = "pay_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_name: Mapped[str] = mapped_column(String(150), nullable=False)
    pay_frequency: Mapped[str] = mapped_column(String(30), nullable=False)  # Monthly/Semi-Monthly/Bi-Weekly/Weekly
    # Shape varies by frequency (e.g. {"type": "LastDayOfMonth"} or
    # {"dayOfWeek": "Friday", "firstPayDate": "2025-04-30"}) — stored/returned
    # as-is, not over-validated in Phase 1.
    payment_day_rule: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    holiday_adjustment: Mapped[str] = mapped_column(String(20), nullable=False)  # Before/After
    processing_day_offset: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    first_effective_pay_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # null = applies to all employees; otherwise {"departments": [...], "locations": [...]}
    applies_to: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONType, nullable=True)
