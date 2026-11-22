from datetime import date, datetime
from decimal import Decimal
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.services.attendance.mobile import MAX_BATCH, normalize_bssid


class OfficeLocationFields(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    work_location_id: Optional[int] = None
    latitude: Decimal = Field(ge=-90, le=90)
    longitude: Decimal = Field(ge=-180, le=180)
    radius_m: int = Field(default=100, ge=10, le=5000)
    wifi_bssids: list[str] = []
    active: bool = True

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Name is required")
        return value

    @field_validator("wifi_bssids")
    @classmethod
    def _bssids(cls, value: list[str]) -> list[str]:
        out = []
        for raw in value:
            bssid = normalize_bssid(raw)
            if bssid and bssid not in out:
                out.append(bssid)
        return out


class OfficeLocationRead(OfficeLocationFields):
    id: int
    work_location_name: Optional[str] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class MobileSettingsFields(BaseModel):
    max_upload_age_days: int = Field(ge=1, le=60)
    max_accuracy_m: int = Field(ge=10, le=2000)
    clock_tolerance_minutes: int = Field(ge=1, le=120)
    allow_web_punch: bool = False


class MobileSettingsRead(MobileSettingsFields):
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None

    model_config = {"from_attributes": True}


class DeviceRegisterRequest(BaseModel):
    device_uuid: str = Field(min_length=8, max_length=64)
    model: Optional[str] = Field(default=None, max_length=150)
    platform: Optional[str] = Field(default=None, max_length=30)


class DeviceRegisterResponse(BaseModel):
    device_id: int
    device_token: str
    registered_at: datetime


class MobileDeviceRead(BaseModel):
    id: int
    employee_id: int
    employee_code: Optional[str] = None
    employee_name: Optional[str] = None
    device_uuid: str
    model: Optional[str] = None
    platform: Optional[str] = None
    registered_at: datetime
    last_seen_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    revoked_by: Optional[str] = None


class MobilePunchIn(BaseModel):
    client_uuid: str = Field(min_length=8, max_length=64)
    punch_type: Literal["IN", "OUT"]
    device_time: datetime
    trusted_time: Optional[datetime] = None
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    accuracy_m: Optional[float] = Field(default=None, ge=0)
    is_mock: bool = False
    clock_suspect: bool = False
    wifi_bssid: Optional[str] = None
    captured_offline: bool = False

    @field_validator("wifi_bssid")
    @classmethod
    def _bssid(cls, value: Optional[str]) -> Optional[str]:
        try:
            return normalize_bssid(value)
        except ValueError:
            return None


class MobilePunchBatch(BaseModel):
    punches: list[MobilePunchIn] = Field(min_length=1, max_length=MAX_BATCH)


class MobilePunchResult(BaseModel):
    client_uuid: str
    id: int
    status: str
    flags: list[str] = []
    reason: Optional[str] = None
    punch_type: str
    punch_time: datetime
    office_name: Optional[str] = None
    distance_m: Optional[float] = None
    duplicate: bool = False


class MobilePunchRead(BaseModel):
    id: int
    client_uuid: str
    employee_id: int
    employee_code: str
    employee_name: Optional[str] = None
    department: Optional[str] = None
    source: str
    device_model: Optional[str] = None
    punch_type: str
    punch_time: datetime
    device_time: datetime
    trusted_time: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_m: Optional[float] = None
    is_mock: bool
    clock_suspect: bool
    wifi_bssid: Optional[str] = None
    wifi_match: bool
    office_name: Optional[str] = None
    distance_m: Optional[float] = None
    captured_offline: bool
    received_at: datetime
    status: str
    flags: list[str] = []
    reason: Optional[str] = None
    review_action: Optional[str] = None
    reviewed_by: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    review_note: Optional[str] = None


class MobilePunchCounts(BaseModel):
    flagged: int
    rejected: int
    accepted: int


class ReviewRequest(BaseModel):
    note: Optional[str] = Field(default=None, max_length=1000)


class OfficeGeofence(BaseModel):
    id: int
    name: str
    latitude: float
    longitude: float
    radius_m: int
    wifi_bssids: list[str] = []


class MobileRules(BaseModel):
    max_upload_age_days: int
    max_accuracy_m: int
    clock_tolerance_minutes: int
    allow_web_punch: bool


class TodayTimes(BaseModel):
    date: date
    punch_in: Optional[datetime] = None
    punch_out: Optional[datetime] = None


class MobileConfig(BaseModel):
    server_time: datetime
    employee_id: int
    employee_code: str
    employee_name: Optional[str] = None
    device_registered: bool
    device_id: Optional[int] = None
    device_model: Optional[str] = None
    offices: list[OfficeGeofence]
    rules: MobileRules
    today: TodayTimes


class LocationTestRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    accuracy_m: Optional[float] = Field(default=None, ge=0)
    wifi_bssid: Optional[str] = None
    employee_id: Optional[int] = None


class LocationTestOffice(BaseModel):
    id: int
    name: str
    work_location_name: Optional[str] = None
    radius_m: int
    distance_m: float
    inside: bool
    applies_to_employee: Optional[bool] = None


class LocationTestResult(BaseModel):
    offices: list[LocationTestOffice]
    employee_name: Optional[str] = None
    status: Optional[str] = None
    reason: Optional[str] = None
    flags: list[str] = []
