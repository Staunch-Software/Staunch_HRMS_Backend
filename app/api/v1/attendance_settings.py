"""HR attendance policy settings, the holiday calendar, the monthly
Saturday off and the Matrix device modes."""
import re
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_hr
from app.models.attendance import DEVICE_MODES, AttendanceDevice
from app.models.attendance_policy import SATURDAY_OFF_HR_MONTHLY, AttendancePolicy, Holiday, SaturdayOff
from app.models.org import WorkLocation
from app.models.user import AdminUser
from app.schemas.attendance_policy import (
    AttendancePolicyBundle,
    AttendancePolicyFields,
    AttendancePolicyOverrideCreate,
    AttendancePolicyRead,
    AttendancePolicyUpdate,
    AttendanceDeviceRead,
    AttendanceDeviceUpdate,
    AttendanceDeviceUpdateResult,
    HolidayCreate,
    HolidayImportError,
    HolidayImportResult,
    HolidayRead,
    HolidayUpdate,
    SaturdayOffMonth,
    SaturdayOffUpsert,
    SaturdayOffYear,
)
from app.services.attendance.devices import list_devices
from app.services.attendance.recompute import recompute_recent
from app.services.excel_import.common import clean_str
from app.services.excel_import.excel_reader import ExcelParseError, read_workbook_as_records
from app.services.leave.calendar import (
    assumed_saturday_off,
    ensure_default_policy,
    policy_for_location,
    saturdays_in_month,
)

router = APIRouter(prefix="/attendance-settings", tags=["attendance-settings"])

POLICY_FIELDS = tuple(AttendancePolicyFields.model_fields)


def _apply(db: Session, policy: AttendancePolicy, payload: AttendancePolicyFields, user: AdminUser) -> None:
    for field in POLICY_FIELDS:
        setattr(policy, field, getattr(payload, field))
    policy.updated_by = user.id


@router.get("/policy", response_model=AttendancePolicyBundle)
def get_policy(db: Session = Depends(get_db)):
    default = ensure_default_policy(db)
    db.commit()
    overrides = (
        db.query(AttendancePolicy)
        .filter(AttendancePolicy.work_location_id.isnot(None))
        .order_by(AttendancePolicy.id)
        .all()
    )
    return AttendancePolicyBundle(
        default=AttendancePolicyRead.from_policy(default),
        overrides=[AttendancePolicyRead.from_policy(p) for p in overrides],
    )


@router.put("/policy", response_model=AttendancePolicyRead)
def update_default_policy(
    payload: AttendancePolicyUpdate, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)
):
    policy = ensure_default_policy(db)
    _apply(db, policy, payload, user)
    db.commit()
    db.refresh(policy)
    return AttendancePolicyRead.from_policy(policy)


@router.post("/policy/overrides", response_model=AttendancePolicyRead, status_code=status.HTTP_201_CREATED)
def create_override(
    payload: AttendancePolicyOverrideCreate, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)
):
    if db.get(WorkLocation, payload.work_location_id) is None:
        raise HTTPException(status_code=400, detail="Work location not found.")
    exists = db.query(AttendancePolicy).filter(AttendancePolicy.work_location_id == payload.work_location_id).first()
    if exists:
        raise HTTPException(status_code=409, detail="This location already has a policy override.")
    policy = AttendancePolicy(work_location_id=payload.work_location_id)
    _apply(db, policy, payload, user)
    db.add(policy)
    db.commit()
    db.refresh(policy)
    return AttendancePolicyRead.from_policy(policy)


def _get_override(db: Session, policy_id: int) -> AttendancePolicy:
    policy = db.get(AttendancePolicy, policy_id)
    if policy is None or policy.work_location_id is None:
        raise HTTPException(status_code=404, detail="Location override not found.")
    return policy


@router.put("/policy/overrides/{policy_id}", response_model=AttendancePolicyRead)
def update_override(
    policy_id: int, payload: AttendancePolicyUpdate, db: Session = Depends(get_db),
    user: AdminUser = Depends(require_hr),
):
    policy = _get_override(db, policy_id)
    _apply(db, policy, payload, user)
    db.commit()
    db.refresh(policy)
    return AttendancePolicyRead.from_policy(policy)


@router.delete("/policy/overrides/{policy_id}")
def delete_override(policy_id: int, db: Session = Depends(get_db)):
    policy = _get_override(db, policy_id)
    db.delete(policy)
    db.commit()
    return {"success": True}


# --- holidays ------------------------------------------------------------

def _check_location(db: Session, work_location_id: Optional[int]) -> None:
    if work_location_id is not None and db.get(WorkLocation, work_location_id) is None:
        raise HTTPException(status_code=400, detail="Work location not found.")


def _duplicate(db: Session, day: date, work_location_id: Optional[int], exclude_id: Optional[int] = None) -> bool:
    query = db.query(Holiday).filter(Holiday.date == day)
    query = query.filter(
        Holiday.work_location_id.is_(None) if work_location_id is None else Holiday.work_location_id == work_location_id
    )
    if exclude_id is not None:
        query = query.filter(Holiday.id != exclude_id)
    return query.first() is not None


@router.get("/holidays", response_model=list[HolidayRead])
def list_holidays(
    year: Optional[int] = None, work_location_id: Optional[int] = None, db: Session = Depends(get_db)
):
    query = db.query(Holiday)
    if year is not None:
        query = query.filter(Holiday.date >= date(year, 1, 1), Holiday.date <= date(year, 12, 31))
    if work_location_id is not None:
        query = query.filter(Holiday.work_location_id == work_location_id)
    return [HolidayRead.from_holiday(h) for h in query.order_by(Holiday.date, Holiday.id).all()]


@router.post("/holidays", response_model=HolidayRead, status_code=status.HTTP_201_CREATED)
def create_holiday(payload: HolidayCreate, db: Session = Depends(get_db)):
    _check_location(db, payload.work_location_id)
    if _duplicate(db, payload.date, payload.work_location_id):
        raise HTTPException(status_code=409, detail="A holiday already exists on this date for this location.")
    holiday = Holiday(**payload.model_dump())
    db.add(holiday)
    db.commit()
    db.refresh(holiday)
    return HolidayRead.from_holiday(holiday)


@router.put("/holidays/{holiday_id}", response_model=HolidayRead)
def update_holiday(holiday_id: int, payload: HolidayUpdate, db: Session = Depends(get_db)):
    holiday = db.get(Holiday, holiday_id)
    if holiday is None:
        raise HTTPException(status_code=404, detail="Holiday not found.")
    _check_location(db, payload.work_location_id)
    if _duplicate(db, payload.date, payload.work_location_id, exclude_id=holiday_id):
        raise HTTPException(status_code=409, detail="A holiday already exists on this date for this location.")
    for field, value in payload.model_dump().items():
        setattr(holiday, field, value)
    db.commit()
    db.refresh(holiday)
    return HolidayRead.from_holiday(holiday)


@router.delete("/holidays/{holiday_id}")
def delete_holiday(holiday_id: int, db: Session = Depends(get_db)):
    holiday = db.get(Holiday, holiday_id)
    if holiday is None:
        raise HTTPException(status_code=404, detail="Holiday not found.")
    db.delete(holiday)
    db.commit()
    return {"success": True}


DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y",
                "%d-%b-%y", "%d/%m/%y")
ALL_LOCATIONS = {"", "all", "all locations", "*"}


def parse_cell_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = re.sub(r"\s+", " ", str(value).strip())
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _column(record: dict, *names: str):
    lowered = {str(k).strip().lower(): v for k, v in record.items()}
    for name in names:
        if name in lowered:
            return lowered[name]
    return None


@router.post("/holidays/import", response_model=HolidayImportResult)
async def import_holidays(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Columns: Date, Holiday Name, optional Location (blank/All = every
    location). Rows for a date+location that already exist are skipped."""
    try:
        records = read_workbook_as_records(await file.read())
    except ExcelParseError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not records:
        raise HTTPException(status_code=400, detail="The file has no data rows.")
    headers = {str(k).strip().lower() for k in records[0]}
    if "date" not in headers or not ({"holiday name", "name", "holiday"} & headers):
        raise HTTPException(status_code=400, detail="The sheet must have 'Date' and 'Holiday Name' columns.")

    locations = {loc.name.strip().lower(): loc for loc in db.query(WorkLocation).all()}
    created = skipped = 0
    errors: list[HolidayImportError] = []
    seen: set[tuple[date, Optional[int]]] = set()
    for index, record in enumerate(records, start=2):
        raw_date = _column(record, "date")
        day = parse_cell_date(raw_date)
        name = clean_str(_column(record, "holiday name", "name", "holiday"))
        location_text = (clean_str(_column(record, "location", "work location")) or "").strip()
        if day is None:
            errors.append(HolidayImportError(row_number=index, message=f"Invalid or missing date: {raw_date!r}."))
            continue
        if not name:
            errors.append(HolidayImportError(row_number=index, message="Holiday name is required."))
            continue
        location_id = None
        if location_text.lower() not in ALL_LOCATIONS:
            location = locations.get(location_text.lower())
            if location is None:
                errors.append(HolidayImportError(row_number=index, message=f"Unknown location '{location_text}'."))
                continue
            location_id = location.id
        key = (day, location_id)
        if key in seen or _duplicate(db, day, location_id):
            skipped += 1
            continue
        seen.add(key)
        db.add(Holiday(date=day, name=name[:150], work_location_id=location_id, is_active=True))
        created += 1
    db.commit()
    return HolidayImportResult(created=created, skipped=skipped, errors=errors)


# --- Saturday off (HR_MONTHLY policies) ------------------------------------

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _parse_month(month: str) -> tuple[int, int]:
    match = _MONTH_RE.match((month or "").strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise HTTPException(status_code=400, detail="month must be in YYYY-MM format.")
    return int(match.group(1)), int(match.group(2))


def _saturday_rows(db: Session, months: list[str]) -> list[SaturdayOff]:
    return db.query(SaturdayOff).filter(SaturdayOff.month.in_(months)).all()


def _month_read(year: int, month: int, rows: list[SaturdayOff], work_location_id: Optional[int]) -> SaturdayOffMonth:
    key = f"{year:04d}-{month:02d}"
    own = next((r.off_date for r in rows if r.month == key and r.work_location_id == work_location_id), None)
    inherited = None
    if work_location_id is not None:
        inherited = next((r.off_date for r in rows if r.month == key and r.work_location_id is None), None)
    assumed = assumed_saturday_off(year, month)
    return SaturdayOffMonth(
        month=key, saturdays=saturdays_in_month(year, month), off_date=own, inherited_date=inherited,
        assumed_date=assumed, effective_date=own or inherited or assumed, work_location_id=work_location_id,
    )


@router.get("/saturday-off", response_model=SaturdayOffYear)
def list_saturday_off(
    year: int = Query(..., ge=2000, le=2100), work_location_id: Optional[int] = None, db: Session = Depends(get_db)
):
    """The 12 months of `year`: HR's chosen Saturday off (null when not
    chosen) and the 2nd Saturday assumed until it is."""
    _check_location(db, work_location_id)
    rows = _saturday_rows(db, [f"{year:04d}-{m:02d}" for m in range(1, 13)])
    policy = policy_for_location(db, work_location_id)
    return SaturdayOffYear(
        year=year, work_location_id=work_location_id,
        saturday_off_mode=policy.saturday_off_mode or SATURDAY_OFF_HR_MONTHLY,
        months=[_month_read(year, m, rows, work_location_id) for m in range(1, 13)],
    )


@router.put("/saturday-off/{month}", response_model=SaturdayOffMonth)
def set_saturday_off(
    month: str, payload: SaturdayOffUpsert, db: Session = Depends(get_db), user: AdminUser = Depends(require_hr)
):
    year, mon = _parse_month(month)
    key = f"{year:04d}-{mon:02d}"
    if payload.off_date.weekday() != 5 or (payload.off_date.year, payload.off_date.month) != (year, mon):
        raise HTTPException(status_code=400, detail=f"off_date must be a Saturday in {key}.")
    _check_location(db, payload.work_location_id)
    query = db.query(SaturdayOff).filter(SaturdayOff.month == key)
    query = query.filter(
        SaturdayOff.work_location_id.is_(None) if payload.work_location_id is None
        else SaturdayOff.work_location_id == payload.work_location_id
    )
    row = query.first()
    if row is None:
        row = SaturdayOff(month=key, work_location_id=payload.work_location_id)
        db.add(row)
    row.off_date = payload.off_date
    row.updated_by = user.username
    db.commit()
    return _month_read(year, mon, _saturday_rows(db, [key]), payload.work_location_id)


@router.delete("/saturday-off/{month}")
def delete_saturday_off(month: str, work_location_id: Optional[int] = None, db: Session = Depends(get_db)):
    year, mon = _parse_month(month)
    key = f"{year:04d}-{mon:02d}"
    query = db.query(SaturdayOff).filter(SaturdayOff.month == key)
    query = query.filter(
        SaturdayOff.work_location_id.is_(None) if work_location_id is None
        else SaturdayOff.work_location_id == work_location_id
    )
    row = query.first()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No Saturday off chosen for {key}.")
    db.delete(row)
    db.commit()
    return {"success": True}


# --- Matrix devices ----------------------------------------------------------

@router.get("/devices", response_model=list[AttendanceDeviceRead])
def get_devices(db: Session = Depends(get_db)):
    """Every Matrix device, including names only seen in punches (added as IN_OUT)."""
    devices = list_devices(db)
    db.commit()
    return devices


@router.put("/devices/{device_id}", response_model=AttendanceDeviceUpdateResult)
def update_device(
    device_id: int, payload: AttendanceDeviceUpdate, db: Session = Depends(get_db),
    user: AdminUser = Depends(require_hr),
):
    """A mode change recomputes the daily rows of the last 62 days."""
    device = db.get(AttendanceDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found.")
    mode = (payload.mode or "").strip().upper()
    if mode not in DEVICE_MODES:
        raise HTTPException(status_code=400, detail=f"mode must be one of {', '.join(DEVICE_MODES)}.")
    changed = mode != device.mode
    device.mode = mode
    if payload.display_name is not None:
        device.display_name = payload.display_name.strip() or None
    if payload.is_active is not None:
        device.is_active = payload.is_active
    device.updated_by = user.username
    db.flush()
    result = {"recomputed_from": None, "recomputed_to": None, "days_recomputed": 0}
    if changed:
        date_from, date_to, count = recompute_recent(db)
        result = {"recomputed_from": date_from, "recomputed_to": date_to, "days_recomputed": count}
    db.commit()
    db.refresh(device)
    return AttendanceDeviceUpdateResult(device=AttendanceDeviceRead.model_validate(device), **result)
