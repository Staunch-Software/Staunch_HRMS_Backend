"""HR side of app attendance: office geofences, validation limits, the
review queue of mobile punches and registered phones."""
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.me_mobile import punch_read
from app.core.db import get_db
from app.core.security import require_hr
from app.models.employee import Employee
from app.models.mobile_attendance import (
    PUNCH_ACCEPTED,
    PUNCH_FLAGGED,
    PUNCH_IN,
    PUNCH_REJECTED,
    PUNCH_STATUSES,
    SOURCE_APP,
    MobileDevice,
    MobilePunch,
    OfficeLocation,
)
from app.models.org import WorkLocation
from app.models.user import AdminUser
from app.schemas.mobile_attendance import (
    LocationTestOffice,
    LocationTestRequest,
    LocationTestResult,
    MobileDeviceRead,
    MobilePunchCounts,
    MobilePunchRead,
    MobileSettingsFields,
    MobileSettingsRead,
    OfficeLocationFields,
    OfficeLocationRead,
    ReviewRequest,
)
from app.services.attendance import mobile
from app.services.reporting import employee_full_name

router = APIRouter(tags=["mobile-attendance"], dependencies=[Depends(require_hr)])


def _office_read(office: OfficeLocation) -> OfficeLocationRead:
    return OfficeLocationRead(
        id=office.id, name=office.name, work_location_id=office.work_location_id,
        work_location_name=office.work_location.name if office.work_location else None,
        latitude=office.latitude, longitude=office.longitude, radius_m=office.radius_m,
        wifi_bssids=office.wifi_bssids or [], active=office.active, updated_at=office.updated_at,
        updated_by=office.updated_by,
    )


def _apply_office(db: Session, office: OfficeLocation, payload: OfficeLocationFields, user: AdminUser) -> None:
    if payload.work_location_id is not None and db.get(WorkLocation, payload.work_location_id) is None:
        raise HTTPException(status_code=400, detail="Work location not found")
    for field, value in payload.model_dump().items():
        setattr(office, field, value)
    office.updated_by = user.username


@router.get("/office-locations", response_model=list[OfficeLocationRead])
def list_office_locations(db: Session = Depends(get_db)):
    rows = db.scalars(select(OfficeLocation).order_by(OfficeLocation.name, OfficeLocation.id)).all()
    return [_office_read(o) for o in rows]


@router.post("/office-locations", response_model=OfficeLocationRead, status_code=status.HTTP_201_CREATED)
def create_office_location(payload: OfficeLocationFields, user: AdminUser = Depends(require_hr),
                           db: Session = Depends(get_db)):
    office = OfficeLocation()
    _apply_office(db, office, payload, user)
    db.add(office)
    db.commit()
    db.refresh(office)
    return _office_read(office)


def _get_office(db: Session, office_id: int) -> OfficeLocation:
    office = db.get(OfficeLocation, office_id)
    if office is None:
        raise HTTPException(status_code=404, detail="Office location not found")
    return office


@router.put("/office-locations/{office_id}", response_model=OfficeLocationRead)
def update_office_location(office_id: int, payload: OfficeLocationFields, user: AdminUser = Depends(require_hr),
                           db: Session = Depends(get_db)):
    office = _get_office(db, office_id)
    _apply_office(db, office, payload, user)
    db.commit()
    db.refresh(office)
    return _office_read(office)


@router.delete("/office-locations/{office_id}")
def delete_office_location(office_id: int, db: Session = Depends(get_db)):
    office = _get_office(db, office_id)
    db.query(MobilePunch).filter(MobilePunch.office_location_id == office_id).update(
        {MobilePunch.office_location_id: None})
    db.delete(office)
    db.commit()
    return {"deleted": office_id}


@router.post("/office-locations/test", response_model=LocationTestResult)
def test_office_location(payload: LocationTestRequest, db: Session = Depends(get_db)):
    """Dry run of the app's location check for a point: distance to every
    active office and, for an employee, the result a punch there would get.
    Nothing is saved."""
    employee = None
    if payload.employee_id is not None:
        employee = db.get(Employee, payload.employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="Employee not found")
    allowed = {o.id for o in mobile.candidate_offices(db, employee)} if employee else None
    offices = db.scalars(select(OfficeLocation).where(OfficeLocation.active.is_(True)).order_by(OfficeLocation.id))
    rows = []
    for office in offices:
        distance = mobile.haversine_m(payload.latitude, payload.longitude, float(office.latitude), float(office.longitude))
        rows.append(LocationTestOffice(
            id=office.id, name=office.name,
            work_location_name=office.work_location.name if office.work_location else None,
            radius_m=office.radius_m, distance_m=round(distance, 1), inside=distance <= office.radius_m,
            applies_to_employee=(office.id in allowed) if allowed is not None else None,
        ))
    rows.sort(key=lambda r: r.distance_m)
    result = LocationTestResult(offices=rows)
    if employee is not None:
        now = datetime.now(mobile.LOCAL_TZ)
        item = mobile.PunchInput(
            client_uuid="location-test", punch_type=PUNCH_IN, device_time=now, trusted_time=now,
            latitude=payload.latitude, longitude=payload.longitude,
            accuracy_m=payload.accuracy_m if payload.accuracy_m is not None else 10.0,
            wifi_bssid=payload.wifi_bssid,
        )
        punch = mobile.evaluate(db, employee, None, item, mobile.get_settings(db), now, SOURCE_APP)
        result.employee_name = employee_full_name(employee)
        result.status, result.reason, result.flags = punch.status, punch.reason, list(punch.flags or [])
    db.rollback()
    return result


@router.get("/mobile-attendance/settings", response_model=MobileSettingsRead)
def read_mobile_settings(db: Session = Depends(get_db)):
    row = mobile.get_settings(db)
    db.commit()
    return row


@router.put("/mobile-attendance/settings", response_model=MobileSettingsRead)
def update_mobile_settings(payload: MobileSettingsFields, user: AdminUser = Depends(require_hr),
                           db: Session = Depends(get_db)):
    row = mobile.get_settings(db)
    for field, value in payload.model_dump().items():
        setattr(row, field, value)
    row.updated_by = user.username
    db.commit()
    db.refresh(row)
    return row


@router.get("/mobile-punches", response_model=list[MobilePunchRead])
def list_mobile_punches(
    status_filter: Optional[str] = Query(None, alias="status", description="ACCEPTED|FLAGGED|REJECTED"),
    employee_id: Optional[int] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    stmt = select(MobilePunch)
    if status_filter:
        wanted = status_filter.strip().upper()
        if wanted not in PUNCH_STATUSES:
            raise HTTPException(status_code=400, detail=f"status must be one of {', '.join(PUNCH_STATUSES)}")
        stmt = stmt.where(MobilePunch.status == wanted)
    if employee_id is not None:
        stmt = stmt.where(MobilePunch.employee_id == employee_id)
    if date_from is not None:
        stmt = stmt.where(MobilePunch.punch_time >= datetime.combine(date_from, datetime.min.time()))
    if date_to is not None:
        stmt = stmt.where(MobilePunch.punch_time < datetime.combine(date_to + timedelta(days=1), datetime.min.time()))
    rows = db.scalars(stmt.order_by(MobilePunch.punch_time.desc(), MobilePunch.id.desc()).limit(limit)).all()
    return [punch_read(p) for p in rows]


@router.get("/mobile-punches/counts", response_model=MobilePunchCounts)
def mobile_punch_counts(db: Session = Depends(get_db)):
    counts = dict(db.execute(select(MobilePunch.status, func.count()).group_by(MobilePunch.status)).all())
    return MobilePunchCounts(flagged=counts.get(PUNCH_FLAGGED, 0), rejected=counts.get(PUNCH_REJECTED, 0),
                             accepted=counts.get(PUNCH_ACCEPTED, 0))


def _get_punch(db: Session, punch_id: int) -> MobilePunch:
    punch = db.get(MobilePunch, punch_id)
    if punch is None:
        raise HTTPException(status_code=404, detail="Mobile punch not found")
    return punch


@router.post("/mobile-punches/{punch_id}/approve", response_model=MobilePunchRead)
def approve_mobile_punch(punch_id: int, payload: ReviewRequest, user: AdminUser = Depends(require_hr),
                         db: Session = Depends(get_db)):
    try:
        punch = mobile.approve(db, _get_punch(db, punch_id), user.username, payload.note)
    except mobile.MobileError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return punch_read(punch)


@router.post("/mobile-punches/{punch_id}/reject", response_model=MobilePunchRead)
def reject_mobile_punch(punch_id: int, payload: ReviewRequest, user: AdminUser = Depends(require_hr),
                        db: Session = Depends(get_db)):
    try:
        punch = mobile.reject(db, _get_punch(db, punch_id), user.username, payload.note)
    except mobile.MobileError as exc:
        db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return punch_read(punch)


def _device_read(device: MobileDevice) -> MobileDeviceRead:
    employee = device.employee
    return MobileDeviceRead(
        id=device.id, employee_id=device.employee_id,
        employee_code=employee.employee_code if employee else None, employee_name=employee_full_name(employee),
        device_uuid=device.device_uuid, model=device.model, platform=device.platform,
        registered_at=device.registered_at, last_seen_at=device.last_seen_at, revoked_at=device.revoked_at,
        revoked_by=device.revoked_by,
    )


@router.get("/mobile-devices", response_model=list[MobileDeviceRead])
def list_mobile_devices(employee_id: Optional[int] = None, active_only: bool = True, db: Session = Depends(get_db)):
    stmt = select(MobileDevice)
    if employee_id is not None:
        stmt = stmt.where(MobileDevice.employee_id == employee_id)
    if active_only:
        stmt = stmt.where(MobileDevice.revoked_at.is_(None))
    return [_device_read(d) for d in db.scalars(stmt.order_by(MobileDevice.registered_at.desc())).all()]


@router.post("/mobile-devices/{employee_id}/reset", response_model=Optional[MobileDeviceRead])
def reset_mobile_device(employee_id: int, user: AdminUser = Depends(require_hr), db: Session = Depends(get_db)):
    if db.get(Employee, employee_id) is None:
        raise HTTPException(status_code=404, detail="Employee not found")
    device = mobile.revoke_device(db, employee_id, user.username)
    db.commit()
    return _device_read(device) if device else None
