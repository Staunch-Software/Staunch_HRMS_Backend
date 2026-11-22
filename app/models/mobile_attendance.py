"""Mobile (app) attendance: office geofences, registered phones and the audit
of every punch attempt. Accepted punches are copied into attendance_punches
with device_name MOBILE_DEVICE_NAME; see app.services.attendance.mobile."""
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, false, text, true
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, _utcnow
from app.models.json_type import JSONType

PUNCH_ACCEPTED = "ACCEPTED"
PUNCH_FLAGGED = "FLAGGED"
PUNCH_REJECTED = "REJECTED"
PUNCH_STATUSES = (PUNCH_ACCEPTED, PUNCH_FLAGGED, PUNCH_REJECTED)
PUNCH_IN = "IN"
PUNCH_OUT = "OUT"
SOURCE_APP = "APP"
SOURCE_WEB = "WEB"

SETTINGS_DEFAULTS = {
    "max_upload_age_days": 7,
    "max_accuracy_m": 100,
    "clock_tolerance_minutes": 5,
    "allow_web_punch": False,
}


class OfficeLocation(Base, TimestampMixin):
    __tablename__ = "office_locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    # NULL = any employee may punch here.
    work_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("work_locations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    latitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(9, 6), nullable=False)
    radius_m: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    # Router MAC addresses, lowercase aa:bb:cc:dd:ee:ff.
    wifi_bssids: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default=true())
    updated_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    work_location = relationship("WorkLocation")


class MobileDevice(Base):
    """A phone registered for app punching. At most one row per employee has
    revoked_at NULL; the device token is shown once and only its hash kept."""

    __tablename__ = "mobile_devices"
    __table_args__ = (
        Index(
            "uq_mobile_device_active_employee", "employee_id", unique=True,
            postgresql_where=text("revoked_at IS NULL"), sqlite_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    device_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    platform: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    device_token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    employee = relationship("Employee")


class MobilePunch(Base):
    """Every punch attempt from the app (or browser), whatever its outcome."""

    __tablename__ = "mobile_punches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_uuid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="CASCADE"), nullable=False, index=True)
    employee_code: Mapped[str] = mapped_column(String(30), nullable=False)
    device_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("mobile_devices.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(10), nullable=False, default=SOURCE_APP)
    punch_type: Mapped[str] = mapped_column(String(3), nullable=False)  # IN|OUT
    device_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trusted_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # Local (IST) wall-clock time used for attendance, like attendance_punches.
    punch_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    latitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    longitude: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    accuracy_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    is_mock: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    clock_suspect: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    wifi_bssid: Mapped[Optional[str]] = mapped_column(String(17), nullable=True)
    wifi_match: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    office_location_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("office_locations.id", ondelete="SET NULL"), nullable=True
    )
    distance_m: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    captured_offline: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    flags: Mapped[list[Any]] = mapped_column(JSONType, nullable=False, default=list)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    attendance_punch_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("attendance_punches.id", ondelete="SET NULL"), nullable=True
    )
    review_action: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)  # APPROVED|REJECTED
    reviewed_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    review_note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    employee = relationship("Employee")
    device = relationship("MobileDevice")
    office = relationship("OfficeLocation")


class MobileAttendanceSettings(Base):
    """Singleton (id 1) of the mobile punch validation limits."""

    __tablename__ = "mobile_attendance_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    max_upload_age_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7, server_default="7")
    max_accuracy_m: Mapped[int] = mapped_column(Integer, nullable=False, default=100, server_default="100")
    clock_tolerance_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    allow_web_punch: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=false())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )
    updated_by: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)
