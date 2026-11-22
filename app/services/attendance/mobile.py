"""Mobile (app) punches: device registration, server-side validation and the
copy of accepted punches into attendance_punches.

Every attempt is kept as a MobilePunch. Outcome:
- REJECTED: not on office Wi-Fi and the GPS fix is outside every office radius
  (or there is no fix / no office to check against).
- FLAGGED: inside (or on office Wi-Fi) but something needs HR's eye: mock
  location, unverifiable clock, poor GPS accuracy (unless on office Wi-Fi),
  uploaded too late, month already sent to payroll, browser punch, or office
  Wi-Fi with a confident GPS fix far away.
- ACCEPTED: everything else. An AttendancePunch (device MOBILE_DEVICE_NAME,
  source MOBILE) is inserted and the day rebuilt by recompute_daily, so the
  Matrix earliest-in / latest-out merge applies unchanged.
HR approving a FLAGGED/REJECTED punch accepts it the same way; rejecting an
ACCEPTED one removes its attendance punch again.
"""
import hashlib
import hmac
import logging
import math
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.attendance import MOBILE_DEVICE_NAME, AttendanceDaily, AttendancePunch
from app.models.attendance_month import AttendanceMonthResult
from app.models.employee import Employee
from app.models.mobile_attendance import (
    PUNCH_ACCEPTED,
    PUNCH_FLAGGED,
    PUNCH_IN,
    PUNCH_REJECTED,
    SETTINGS_DEFAULTS,
    SOURCE_WEB,
    MobileAttendanceSettings,
    MobileDevice,
    MobilePunch,
    OfficeLocation,
)
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.variable_pay import VariablePayInput
from app.services.attendance.sync import IN, OUT, recompute_daily
from app.services.employee_code import normalize_employee_code
from app.services.reporting import employee_full_name

# Punch times are stored as local wall-clock time, like Matrix punches.
logger = logging.getLogger(__name__)

LOCAL_TZ = timezone(timedelta(hours=5, minutes=30))
FINAL_RUN_STATUSES = ("Approved", "Paid")
PUNCH_SOURCE_MOBILE = "MOBILE"
MAX_BATCH = 100

FLAG_MOCK = "MOCK_LOCATION"
FLAG_CLOCK = "CLOCK_SUSPECT"
FLAG_ACCURACY = "POOR_ACCURACY"
FLAG_TOO_OLD = "TOO_OLD"
FLAG_LOCKED = "LOCKED_MONTH"
FLAG_CONFLICT = "LOCATION_CONFLICT"
FLAG_BROWSER = "BROWSER"
REJECT_OUTSIDE = "OUTSIDE_RADIUS"
REJECT_NO_LOCATION = "NO_LOCATION"
REJECT_NO_OFFICE = "NO_OFFICE"

# Android reports these when the BSSID is hidden (no permission / not connected).
_PLACEHOLDER_BSSIDS = {"02:00:00:00:00:00", "00:00:00:00:00:00", "ff:ff:ff:ff:ff:ff"}


class MobileError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def normalize_bssid(value: Optional[str]) -> Optional[str]:
    """aa:bb:cc:dd:ee:ff, or None when blank / a placeholder. Raises
    ValueError on anything else."""
    if value is None:
        return None
    raw = re.sub(r"[\s\-.:]", "", str(value)).lower()
    if not raw:
        return None
    if not re.fullmatch(r"[0-9a-f]{12}", raw):
        raise ValueError(f"{value!r} is not a Wi-Fi BSSID (MAC address like aa:bb:cc:dd:ee:ff)")
    bssid = ":".join(raw[i:i + 2] for i in range(0, 12, 2))
    return None if bssid in _PLACEHOLDER_BSSIDS else bssid


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(a)))


def get_settings(db: Session) -> MobileAttendanceSettings:
    row = db.get(MobileAttendanceSettings, 1)
    if row is None:
        row = MobileAttendanceSettings(id=1, **SETTINGS_DEFAULTS)
        db.add(row)
        db.flush()
    return row


def candidate_offices(db: Session, employee: Employee) -> list[OfficeLocation]:
    """Active offices for the employee's work location plus those open to all;
    every active office when the employee has no work location."""
    stmt = select(OfficeLocation).where(OfficeLocation.active.is_(True)).order_by(OfficeLocation.id)
    if employee.work_location_id is not None:
        stmt = stmt.where(or_(OfficeLocation.work_location_id.is_(None),
                              OfficeLocation.work_location_id == employee.work_location_id))
    return list(db.scalars(stmt))


# --- devices -----------------------------------------------------------------

def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def active_device(db: Session, employee_id: int) -> Optional[MobileDevice]:
    return db.scalar(select(MobileDevice).where(MobileDevice.employee_id == employee_id,
                                                MobileDevice.revoked_at.is_(None)))


def register_device(db: Session, employee: Employee, device_uuid: str, model: Optional[str],
                    platform: Optional[str]) -> tuple[MobileDevice, str]:
    """Registers (or re-keys) the employee's phone and returns it with a new
    device token "<id>.<secret>". A different phone, or a phone already
    registered to another employee, is refused until HR resets the
    registration. Does not commit."""
    device_uuid = device_uuid.strip()
    device = active_device(db, employee.id)
    if device is not None and device.device_uuid != device_uuid:
        raise MobileError(
            f"Attendance is already registered on another phone ({device.model or 'unknown model'}). "
            "Ask HR to reset your mobile device.", 409)
    taken = db.scalar(select(MobileDevice.id).where(MobileDevice.device_uuid == device_uuid,
                                                    MobileDevice.revoked_at.is_(None),
                                                    MobileDevice.employee_id != employee.id).limit(1))
    if taken is not None:
        raise MobileError("This phone is registered for another employee's attendance. "
                          "Ask HR to reset that registration first.", 409)
    secret = secrets.token_urlsafe(32)
    if device is None:
        device = MobileDevice(employee_id=employee.id, device_uuid=device_uuid, device_token_hash="")
        db.add(device)
    device.model = (model or "").strip()[:150] or None
    device.platform = (platform or "").strip()[:30] or None
    device.device_token_hash = _hash_secret(secret)
    device.registered_at = datetime.now(timezone.utc)
    db.flush()
    return device, f"{device.id}.{secret}"


def resolve_device_token(db: Session, token: Optional[str]) -> Optional[MobileDevice]:
    if not token or "." not in token:
        return None
    device_id, _, secret = token.strip().partition(".")
    if not device_id.isdigit() or not secret:
        return None
    device = db.get(MobileDevice, int(device_id))
    if device is None or device.revoked_at is not None:
        return None
    if not hmac.compare_digest(device.device_token_hash, _hash_secret(secret)):
        return None
    return device


def revoke_device(db: Session, employee_id: int, by: Optional[str]) -> Optional[MobileDevice]:
    device = active_device(db, employee_id)
    if device is not None:
        device.revoked_at = datetime.now(timezone.utc)
        device.revoked_by = by
        db.flush()
    return device


# --- validation --------------------------------------------------------------

@dataclass
class PunchInput:
    client_uuid: str
    punch_type: str
    device_time: datetime
    trusted_time: Optional[datetime] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy_m: Optional[float] = None
    is_mock: bool = False
    clock_suspect: bool = False
    wifi_bssid: Optional[str] = None
    captured_offline: bool = False


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value.replace(tzinfo=LOCAL_TZ) if value.tzinfo is None else value


def to_local(value: datetime) -> datetime:
    return _aware(value).astimezone(LOCAL_TZ).replace(tzinfo=None, microsecond=0)


def month_locked(db: Session, employee_id: int, day: date) -> bool:
    """True when payroll for the day is final for the employee: the period's
    variable pay input is Locked, or an Approved or Paid payroll run covering
    the day includes them. A month only sent to payroll is still open."""
    locked = db.scalar(select(VariablePayInput.id).where(
        VariablePayInput.employee_id == employee_id,
        VariablePayInput.period_start <= day,
        VariablePayInput.period_end >= day,
        VariablePayInput.status == "Locked",
    ).limit(1))
    if locked is not None:
        return True
    final_run = db.scalar(select(PayrollRun.id).join(PayrollRunLineItem).where(
        PayrollRunLineItem.employee_id == employee_id,
        PayrollRun.period_start <= day,
        PayrollRun.period_end >= day,
        PayrollRun.status.in_(FINAL_RUN_STATUSES),
    ).limit(1))
    return final_run is not None


def refresh_month_result(db: Session, employee_id: int, day: date) -> None:
    """Re-processes the employee's attendance month when it was already
    processed, so Monthly Summary reflects the new punch (a sent month goes
    back to processed and needs sending again)."""
    from app.services.attendance import monthly

    ref = day.strftime("%Y-%m")
    row = db.scalar(select(AttendanceMonthResult).where(
        AttendanceMonthResult.employee_id == employee_id, AttendanceMonthResult.month == ref))
    if row is None or row.pay_group_id is None:
        return
    try:
        with db.begin_nested():
            monthly.process_month(db, ref, row.pay_group_id, None, employee_ids=[employee_id])
    except Exception:  # noqa: BLE001 - HR can still process the month by hand
        logger.exception("Re-processing %s for employee %s after a mobile punch failed", ref, employee_id)


def _clock_problems(item: PunchInput, tolerance: timedelta, now: datetime) -> Optional[str]:
    device = _aware(item.device_time)
    trusted = _aware(item.trusted_time)
    if item.clock_suspect:
        return "The phone restarted or its clock changed since the last online check"
    if trusted is None:
        return "No server time reference on the phone"
    if abs(trusted - device) > tolerance:
        return f"Phone clock differs from trusted time by {int(abs(trusted - device).total_seconds() // 60)} min"
    if trusted - now > tolerance:
        return "Punch time is in the future"
    if not item.captured_offline and abs(now - trusted) > tolerance:
        return "Online punch time differs from server time"
    return None


def evaluate(db: Session, employee: Employee, device: Optional[MobileDevice], item: PunchInput,
             settings: MobileAttendanceSettings, now: datetime, source: str) -> MobilePunch:
    """Builds the MobilePunch for one attempt with its status, flags and the
    nearest office. Does not add it to the session."""
    tolerance = timedelta(minutes=settings.clock_tolerance_minutes)
    effective = _aware(item.trusted_time) or _aware(item.device_time)
    bssid = normalize_bssid(item.wifi_bssid) if item.wifi_bssid else None
    punch = MobilePunch(
        client_uuid=item.client_uuid,
        employee_id=employee.id,
        employee_code=normalize_employee_code(employee.employee_code),
        device_id=device.id if device else None,
        source=source,
        punch_type=item.punch_type,
        device_time=_aware(item.device_time),
        trusted_time=_aware(item.trusted_time),
        punch_time=to_local(effective),
        latitude=item.latitude,
        longitude=item.longitude,
        accuracy_m=item.accuracy_m,
        is_mock=bool(item.is_mock),
        wifi_bssid=bssid,
        captured_offline=bool(item.captured_offline),
        received_at=now,
    )

    offices = candidate_offices(db, employee)
    has_fix = item.latitude is not None and item.longitude is not None
    nearest, nearest_distance = None, None
    if has_fix:
        for office in offices:
            d = haversine_m(item.latitude, item.longitude, float(office.latitude), float(office.longitude))
            if nearest_distance is None or d < nearest_distance:
                nearest, nearest_distance = office, d
    wifi_office = next((o for o in offices if bssid and bssid in (o.wifi_bssids or [])), None)
    punch.wifi_match = wifi_office is not None
    office = wifi_office or nearest
    punch.office_location_id = office.id if office else None
    if office is not None and has_fix:
        punch.distance_m = round(haversine_m(item.latitude, item.longitude,
                                             float(office.latitude), float(office.longitude)), 1)

    flags, reasons = [], []

    def flag(code: str, reason: str) -> None:
        flags.append(code)
        reasons.append(reason)

    clock_reason = _clock_problems(item, tolerance, now)
    punch.clock_suspect = clock_reason is not None

    if not offices:
        punch.status, punch.flags, punch.reason = PUNCH_REJECTED, [REJECT_NO_OFFICE], \
            "No office location is set up for your work location"
        return punch
    if wifi_office is None:
        if not has_fix:
            punch.status, punch.flags, punch.reason = PUNCH_REJECTED, [REJECT_NO_LOCATION], \
                "No GPS location and not connected to office Wi-Fi"
            return punch
        if punch.distance_m > office.radius_m:
            punch.status, punch.flags, punch.reason = PUNCH_REJECTED, [REJECT_OUTSIDE], \
                f"{punch.distance_m:.0f} m from {office.name}, outside its {office.radius_m} m radius"
            return punch

    if punch.is_mock:
        flag(FLAG_MOCK, "Mock location detected on the phone")
    if clock_reason:
        flag(FLAG_CLOCK, clock_reason)
    accuracy_limit = settings.max_accuracy_m
    if wifi_office is None and (item.accuracy_m is None or item.accuracy_m > accuracy_limit):
        shown = "unknown" if item.accuracy_m is None else f"{item.accuracy_m:.0f} m"
        flag(FLAG_ACCURACY, f"GPS accuracy {shown} is worse than {accuracy_limit} m")
    if (wifi_office is not None and punch.distance_m is not None and item.accuracy_m is not None
            and item.accuracy_m <= accuracy_limit and punch.distance_m - item.accuracy_m > wifi_office.radius_m):
        flag(FLAG_CONFLICT, f"On {wifi_office.name} Wi-Fi but GPS places the phone {punch.distance_m:.0f} m away")
    if now - effective > timedelta(days=settings.max_upload_age_days):
        flag(FLAG_TOO_OLD, f"Uploaded more than {settings.max_upload_age_days} days after the punch")
    if month_locked(db, employee.id, punch.punch_time.date()):
        flag(FLAG_LOCKED, f"{punch.punch_time:%b %Y} payroll is already locked or approved")
    if source == SOURCE_WEB:
        flag(FLAG_BROWSER, "Punched from a browser; mock locations cannot be detected there")

    punch.flags = flags
    punch.status = PUNCH_FLAGGED if flags else PUNCH_ACCEPTED
    punch.reason = "; ".join(reasons) or None
    return punch


# --- attendance punches ------------------------------------------------------

def _rebuild(db: Session, employee: Employee, code: str, day: date) -> None:
    recompute_daily(db, [(code, day)], {code: employee.id})


def insert_attendance_punch(db: Session, punch: MobilePunch, employee: Employee) -> AttendancePunch:
    """Copies an accepted mobile punch into attendance_punches (reusing an
    identical existing row) and rebuilds that day. Does not commit."""
    code = punch.employee_code
    kind = IN if punch.punch_type == PUNCH_IN else OUT
    row = db.scalar(select(AttendancePunch).where(
        AttendancePunch.employee_code == code,
        AttendancePunch.event_datetime == punch.punch_time,
        AttendancePunch.entry_exit_type == kind,
        AttendancePunch.device_name == MOBILE_DEVICE_NAME,
    ))
    if row is None:
        row = AttendancePunch(
            employee_code=code,
            raw_userid=employee.employee_code,
            username=employee_full_name(employee),
            event_datetime=punch.punch_time,
            event_date=punch.punch_time.date(),
            entry_exit_type=kind,
            device_name=MOBILE_DEVICE_NAME,
            received_at=to_local(punch.received_at or datetime.now(timezone.utc)),
            employee_id=employee.id,
            source=PUNCH_SOURCE_MOBILE,
            mobile_punch_id=punch.id,
        )
        db.add(row)
        db.flush()
    punch.attendance_punch_id = row.id
    _rebuild(db, employee, code, punch.punch_time.date())
    refresh_month_result(db, employee.id, punch.punch_time.date())
    return row


def remove_attendance_punch(db: Session, punch: MobilePunch, employee: Employee) -> None:
    if punch.attendance_punch_id is None:
        return
    row = db.get(AttendancePunch, punch.attendance_punch_id)
    punch.attendance_punch_id = None
    if row is not None and row.source == PUNCH_SOURCE_MOBILE:
        db.delete(row)
        db.flush()
    _rebuild(db, employee, punch.employee_code, punch.punch_time.date())
    refresh_month_result(db, employee.id, punch.punch_time.date())


@dataclass
class SubmitResult:
    punch: MobilePunch
    duplicate: bool


def submit_punches(db: Session, employee: Employee, device: Optional[MobileDevice], items: Iterable[PunchInput],
                   source: str, now: Optional[datetime] = None) -> list[SubmitResult]:
    """Validates and stores a batch. A client_uuid seen before returns the
    stored outcome instead of a new attempt. Commits."""
    now = now or datetime.now(timezone.utc)
    settings = get_settings(db)
    results: list[SubmitResult] = []
    for item in items:
        existing = db.scalar(select(MobilePunch).where(MobilePunch.client_uuid == item.client_uuid))
        if existing is not None:
            if existing.employee_id != employee.id:
                raise MobileError("Punch id already used", 409)
            results.append(SubmitResult(existing, True))
            continue
        punch = evaluate(db, employee, device, item, settings, now, source)
        db.add(punch)
        db.flush()
        if punch.status == PUNCH_ACCEPTED:
            insert_attendance_punch(db, punch, employee)
        results.append(SubmitResult(punch, False))
    if device is not None:
        device.last_seen_at = now
    db.commit()
    return results


def approve(db: Session, punch: MobilePunch, reviewer: str, note: Optional[str]) -> MobilePunch:
    if punch.status == PUNCH_ACCEPTED:
        raise MobileError("This punch is already accepted")
    employee = db.get(Employee, punch.employee_id)
    punch.status = PUNCH_ACCEPTED
    punch.review_action = "APPROVED"
    punch.reviewed_by, punch.reviewed_at, punch.review_note = reviewer, datetime.now(timezone.utc), note
    insert_attendance_punch(db, punch, employee)
    db.commit()
    return punch


def reject(db: Session, punch: MobilePunch, reviewer: str, note: Optional[str]) -> MobilePunch:
    if punch.status == PUNCH_REJECTED and punch.review_action == "REJECTED":
        raise MobileError("This punch is already rejected")
    employee = db.get(Employee, punch.employee_id)
    if punch.status == PUNCH_ACCEPTED:
        remove_attendance_punch(db, punch, employee)
    punch.status = PUNCH_REJECTED
    punch.review_action = "REJECTED"
    punch.reviewed_by, punch.reviewed_at, punch.review_note = reviewer, datetime.now(timezone.utc), note
    db.commit()
    return punch


def today_times(db: Session, employee: Employee, now: Optional[datetime] = None) -> dict:
    day = to_local(now or datetime.now(timezone.utc)).date()
    row = db.scalar(select(AttendanceDaily).where(
        AttendanceDaily.employee_code == normalize_employee_code(employee.employee_code),
        AttendanceDaily.work_date == day,
    ))
    return {
        "date": day,
        "punch_in": row.punch_in if row else None,
        "punch_out": row.punch_out if row else None,
    }
