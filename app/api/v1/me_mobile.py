"""Employee endpoints for app attendance. Punch upload and config accept the
long-lived device token (X-Device-Token) as well as the session token, so
punches queued offline still upload after the 8 h session expires."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings as app_settings
from app.core.db import get_db
from app.core.security import decode_access_token, login_block_reason, require_employee_user
from app.models.employee import Employee
from app.models.mobile_attendance import SOURCE_APP, SOURCE_WEB, MobileDevice, MobilePunch
from app.models.user import AdminUser
from app.schemas.mobile_attendance import (
    DeviceRegisterRequest,
    DeviceRegisterResponse,
    MobileConfig,
    MobilePunchBatch,
    MobilePunchRead,
    MobilePunchResult,
    MobileRules,
    OfficeGeofence,
    TodayTimes,
)
from app.services.attendance import mobile
from app.services.reporting import employee_full_name

router = APIRouter(prefix="/me", tags=["self-service"])

_optional_bearer = OAuth2PasswordBearer(tokenUrl=f"{app_settings.API_V1_PREFIX}/auth/login", auto_error=False)
DEVICE_TOKEN_HEADER = "X-Device-Token"


@dataclass
class MobileCaller:
    employee: Employee
    device: Optional[MobileDevice]
    source: str


def _unauthorized(detail: str = "Could not validate credentials") -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def _session_user(db: Session, token: Optional[str]) -> Optional[AdminUser]:
    if not token:
        return None
    try:
        username = decode_access_token(token)
    except HTTPException:
        return None
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if user is None or not user.is_active or login_block_reason(user) or user.employee_id is None:
        return None
    return user


def mobile_caller(
    token: Optional[str] = Depends(_optional_bearer),
    device_token: Optional[str] = Header(None, alias=DEVICE_TOKEN_HEADER),
    db: Session = Depends(get_db),
) -> MobileCaller:
    user = _session_user(db, token)
    if device_token:
        device = mobile.resolve_device_token(db, device_token)
        if device is None:
            raise _unauthorized("This phone is no longer registered for attendance. Sign in again.")
        if user is not None and user.employee_id != device.employee_id:
            raise HTTPException(status_code=403, detail="This phone is registered to another employee")
        login = db.query(AdminUser).filter(AdminUser.employee_id == device.employee_id).first()
        if login is None or not login.is_active or login_block_reason(login):
            raise _unauthorized()
        return MobileCaller(login.employee, device, SOURCE_APP)
    if user is None:
        raise _unauthorized()
    if user.must_change_password:
        raise HTTPException(status_code=403, detail="Password change required")
    return MobileCaller(user.employee, None, SOURCE_WEB)


def _punch_result(result: mobile.SubmitResult) -> MobilePunchResult:
    p = result.punch
    return MobilePunchResult(
        client_uuid=p.client_uuid, id=p.id, status=p.status, flags=p.flags or [], reason=p.reason,
        punch_type=p.punch_type, punch_time=p.punch_time, office_name=p.office.name if p.office else None,
        distance_m=p.distance_m, duplicate=result.duplicate,
    )


def punch_read(p: MobilePunch) -> MobilePunchRead:
    employee = p.employee
    return MobilePunchRead(
        id=p.id, client_uuid=p.client_uuid, employee_id=p.employee_id, employee_code=p.employee_code,
        employee_name=employee_full_name(employee),
        department=employee.department.name if employee and employee.department else None,
        source=p.source, device_model=p.device.model if p.device else None,
        punch_type=p.punch_type, punch_time=p.punch_time, device_time=p.device_time, trusted_time=p.trusted_time,
        latitude=p.latitude, longitude=p.longitude, accuracy_m=p.accuracy_m, is_mock=p.is_mock,
        clock_suspect=p.clock_suspect, wifi_bssid=p.wifi_bssid, wifi_match=p.wifi_match,
        office_name=p.office.name if p.office else None, distance_m=p.distance_m,
        captured_offline=p.captured_offline, received_at=p.received_at, status=p.status, flags=p.flags or [],
        reason=p.reason, review_action=p.review_action, reviewed_by=p.reviewed_by, reviewed_at=p.reviewed_at,
        review_note=p.review_note,
    )


@router.post("/mobile-devices/register", response_model=DeviceRegisterResponse)
def register_my_device(
    payload: DeviceRegisterRequest,
    user: AdminUser = Depends(require_employee_user),
    db: Session = Depends(get_db),
):
    try:
        device, token = mobile.register_device(db, user.employee, payload.device_uuid, payload.model,
                                               payload.platform)
    except mobile.MobileError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    db.commit()
    return DeviceRegisterResponse(device_id=device.id, device_token=token, registered_at=device.registered_at)


@router.get("/mobile-config", response_model=MobileConfig)
def read_my_mobile_config(caller: MobileCaller = Depends(mobile_caller), db: Session = Depends(get_db)):
    employee = caller.employee
    settings = mobile.get_settings(db)
    device = caller.device or mobile.active_device(db, employee.id)
    offices = mobile.candidate_offices(db, employee)
    config = MobileConfig(
        server_time=datetime.now(timezone.utc),
        employee_id=employee.id,
        employee_code=employee.employee_code,
        employee_name=employee_full_name(employee),
        device_registered=device is not None,
        device_id=device.id if device else None,
        device_model=device.model if device else None,
        offices=[OfficeGeofence(id=o.id, name=o.name, latitude=float(o.latitude), longitude=float(o.longitude),
                                radius_m=o.radius_m, wifi_bssids=o.wifi_bssids or []) for o in offices],
        rules=MobileRules(max_upload_age_days=settings.max_upload_age_days, max_accuracy_m=settings.max_accuracy_m,
                          clock_tolerance_minutes=settings.clock_tolerance_minutes,
                          allow_web_punch=settings.allow_web_punch),
        today=TodayTimes(**mobile.today_times(db, employee)),
    )
    db.commit()
    return config


@router.post("/mobile-punches", response_model=list[MobilePunchResult])
def submit_my_mobile_punches(
    payload: MobilePunchBatch, caller: MobileCaller = Depends(mobile_caller), db: Session = Depends(get_db)
):
    if caller.source == SOURCE_WEB and not mobile.get_settings(db).allow_web_punch:
        raise HTTPException(status_code=403,
                            detail="Marking attendance from a browser is turned off. Use the HRMS mobile app.")
    items = [mobile.PunchInput(**p.model_dump()) for p in payload.punches]
    try:
        results = mobile.submit_punches(db, caller.employee, caller.device, items, caller.source)
    except mobile.MobileError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return [_punch_result(r) for r in results]


@router.get("/mobile-punches", response_model=list[MobilePunchRead])
def list_my_mobile_punches(
    limit: int = Query(50, ge=1, le=500),
    caller: MobileCaller = Depends(mobile_caller),
    db: Session = Depends(get_db),
):
    rows = db.scalars(
        select(MobilePunch).where(MobilePunch.employee_id == caller.employee.id)
        .order_by(MobilePunch.punch_time.desc(), MobilePunch.id.desc()).limit(limit)
    ).all()
    return [punch_read(p) for p in rows]
