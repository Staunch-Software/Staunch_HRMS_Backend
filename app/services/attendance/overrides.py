"""HR corrections on top of Matrix attendance (AttendanceOverride): value
parsing shared by uploads and manual edits, and the write rule "at most one
active override per (employee_id, work_date); a new one deactivates the
previous, an identical one is a no-op"."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceOverride
from app.services.employee_code import normalize_employee_code

KIND_TIMES = "TIMES"
KIND_STATUS = "STATUS"
KINDS = (KIND_TIMES, KIND_STATUS)

STATUS_LEAVE = "LEAVE"
# OD = on duty (counts as present).
DAY_STATUS_CODES = ("P", "A", "HD", "WO", "H", "OD")
STATUS_CODES = DAY_STATUS_CODES + (STATUS_LEAVE,)
HALVES = ("FULL", "FIRST_HALF", "SECOND_HALF")

SOURCE_UPLOAD_DAILY = "UPLOAD_DAILY"
SOURCE_UPLOAD_MUSTER = "UPLOAD_MUSTER"
SOURCE_MANUAL = "MANUAL"

_UNCHECKED_LEAVE_CODE = re.compile(r"^[A-Z]{1,5}$")
_HD_WITH_LEAVE = re.compile(r"^HD\s*[/\-:+]\s*([A-Z0-9]{1,10})$")
_TIME_TEXT = re.compile(r"^(\d{1,2})(?:[:.](\d{2})(?:[:.](\d{2}))?)?\s*(A\.?\s*M\.?|P\.?\s*M\.?)?$")
_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d-%b-%Y", "%d %b %Y",
    "%d-%b-%y", "%d/%m/%y", "%d-%m-%y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
)
_EXCEL_EPOCH = date(1899, 12, 30)


class OverrideError(ValueError):
    pass


# --- value parsing -------------------------------------------------------------------

def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def parse_time_value(value: Any) -> Optional[time]:
    """Excel time/datetime cells, day fractions (0.375) and text such as
    "09:30", "9:30:00", "9.30", "6:15 PM", "6 PM". Blank -> None; anything
    else raises ValueError."""
    if _blank(value):
        return None
    if isinstance(value, datetime):
        return value.time().replace(microsecond=0)
    if isinstance(value, time):
        return value.replace(microsecond=0)
    if isinstance(value, timedelta):
        if timedelta(0) <= value < timedelta(days=1):
            return (datetime.min + value).time().replace(microsecond=0)
        raise ValueError(f"time {value} is not within one day")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 0 <= value < 1:
            return (datetime.min + timedelta(seconds=round(value * 86400))).time()
        raise ValueError(f"'{value}' is not a time (use HH:MM)")
    text = str(value).strip().upper()
    match = _TIME_TEXT.match(text)
    if not match:
        raise ValueError(f"'{value}' is not a time (use HH:MM or H:MM AM/PM)")
    hour, minute, second, meridiem = match.groups()
    hour, minute, second = int(hour), int(minute or 0), int(second or 0)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"'{value}' is not a valid 12-hour time")
        hour = hour % 12 + (12 if meridiem.startswith("P") else 0)
    elif match.group(2) is None:
        raise ValueError(f"'{value}' is not a time (use HH:MM)")
    if hour > 23 or minute > 59 or second > 59:
        raise ValueError(f"'{value}' is not a valid time")
    return time(hour, minute, second)


def parse_date_value(value: Any) -> Optional[date]:
    """Excel date cells, serial numbers and text in ISO or Indian (DD/MM/YYYY)
    order. Blank -> None; anything else raises ValueError."""
    if _blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 20000 <= value <= 80000:
            return _EXCEL_EPOCH + timedelta(days=int(value))
        raise ValueError(f"'{value}' is not a date")
    text = str(value).strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"'{value}' is not a date (use DD-MM-YYYY or YYYY-MM-DD)")


def format_time(value: Optional[datetime | time]) -> Optional[str]:
    return value.strftime("%H:%M") if value is not None else None


# --- leave codes ---------------------------------------------------------------------

def active_leave_codes(db: Session) -> Optional[set[str]]:
    """Codes of active leave types, or None when leave types are not available
    (model not deployed yet or its table not migrated)."""
    try:
        from app.models.leave import LeaveType
    except ImportError:
        return None
    try:
        if not inspect(db.get_bind()).has_table(LeaveType.__tablename__):
            return None
    except Exception:  # noqa: BLE001 - a missing table must not break attendance
        return None
    stmt = select(LeaveType.code)
    if hasattr(LeaveType, "is_active"):
        stmt = stmt.where(LeaveType.is_active.is_(True))
    return {str(code).strip().upper() for code in db.scalars(stmt) if code}


def check_leave_code(code: str, leave_codes: Optional[set[str]]) -> tuple[str, Optional[str]]:
    """Returns (normalised code, warning). Raises OverrideError for an
    unknown code. Without leave types any 1-5 letter code is accepted."""
    code = (code or "").strip().upper()
    if not code:
        raise OverrideError("leave type code is required")
    if leave_codes is None:
        if not _UNCHECKED_LEAVE_CODE.match(code):
            raise OverrideError(f"'{code}' is not a valid leave code (1-5 letters)")
        return code, f"leave types are not set up yet, so '{code}' was accepted without checking"
    if code not in leave_codes:
        raise OverrideError(f"'{code}' is not an active leave type")
    return code, None


def parse_day_code(raw: Any, leave_codes: Optional[set[str]]) -> tuple[str, Optional[str], Optional[str]]:
    """A muster cell -> (status_code, leave_type_code, warning). P/A/HD/WO/H/OD
    are day statuses, "HD/CL" is a half day with the other half on leave, any
    other code must be a leave type (status LEAVE)."""
    code = str(raw).strip().upper()
    if code in DAY_STATUS_CODES:
        return code, None, None
    hd = _HD_WITH_LEAVE.match(code)
    if hd:
        leave, warning = check_leave_code(hd.group(1), leave_codes)
        return "HD", leave, warning
    try:
        leave, warning = check_leave_code(code, leave_codes)
    except OverrideError as exc:
        raise OverrideError(
            f"'{raw}' is not a day code ({', '.join(DAY_STATUS_CODES)}) or a leave type"
            + ("" if leave_codes is None else f" ({exc})")
        ) from exc
    return STATUS_LEAVE, leave, warning


# --- validation + write ----------------------------------------------------------------

def build_values(
    *,
    kind: str,
    work_date: date,
    punch_in: Optional[time] = None,
    punch_out: Optional[time] = None,
    status_code: Optional[str] = None,
    leave_type_code: Optional[str] = None,
    half: Optional[str] = None,
    reason: Optional[str] = None,
) -> dict[str, Any]:
    """Validated column values for an override. Raises OverrideError. Leave
    codes must already be checked with check_leave_code."""
    kind = (kind or "").strip().upper()
    half = (half or "FULL").strip().upper()
    if kind not in KINDS:
        raise OverrideError(f"kind must be one of {', '.join(KINDS)}")
    if half not in HALVES:
        raise OverrideError(f"half must be one of {', '.join(HALVES)}")
    values: dict[str, Any] = {
        "kind": kind, "punch_in": None, "punch_out": None, "status_code": None,
        "leave_type_code": None, "half": half, "reason": (reason or "").strip() or None,
    }
    if kind == KIND_TIMES:
        if punch_in is None and punch_out is None:
            raise OverrideError("give an in time, an out time or both")
        if punch_in is not None and punch_out is not None and punch_out <= punch_in:
            raise OverrideError("out time must be after in time (overnight shifts are not supported)")
        values["punch_in"] = datetime.combine(work_date, punch_in) if punch_in is not None else None
        values["punch_out"] = datetime.combine(work_date, punch_out) if punch_out is not None else None
        return values
    status = (status_code or "").strip().upper()
    if status not in STATUS_CODES:
        raise OverrideError(f"status_code must be one of {', '.join(STATUS_CODES)}")
    leave = (leave_type_code or "").strip().upper() or None
    if status == STATUS_LEAVE and not leave:
        raise OverrideError("status LEAVE needs a leave_type_code")
    if leave and status not in (STATUS_LEAVE, "HD"):
        raise OverrideError("leave_type_code is only allowed with status LEAVE or HD")
    values["status_code"] = status
    values["leave_type_code"] = leave
    return values


_COMPARED = ("kind", "punch_in", "punch_out", "status_code", "leave_type_code", "half", "reason")
_UNSET: Any = object()


def get_active(db: Session, employee_id: int, work_date: date) -> Optional[AttendanceOverride]:
    return db.scalar(
        select(AttendanceOverride).where(
            AttendanceOverride.employee_id == employee_id,
            AttendanceOverride.work_date == work_date,
            AttendanceOverride.is_active.is_(True),
        )
    )


def _deactivate(row: AttendanceOverride, by: Optional[str]) -> None:
    row.is_active = False
    row.deactivated_at = datetime.now(timezone.utc)
    row.deactivated_by = by


def save_override(
    db: Session,
    employee_id: int,
    employee_code: str,
    work_date: date,
    values: dict[str, Any],
    *,
    source: str,
    created_by: Optional[str],
    import_batch_id: Optional[str] = None,
    current: Any = _UNSET,
) -> tuple[AttendanceOverride, str]:
    """Writes `values` (from build_values) as the active override. Returns
    (row, "created" | "replaced" | "unchanged"). `current` lets bulk callers
    pass a preloaded active row (or None). Does not commit."""
    if current is _UNSET:
        current = get_active(db, employee_id, work_date)
    if current is not None and all(getattr(current, f) == values[f] for f in _COMPARED):
        return current, "unchanged"
    if current is not None:
        _deactivate(current, created_by)
        db.flush()
    row = AttendanceOverride(
        employee_id=employee_id,
        employee_code=normalize_employee_code(employee_code),
        work_date=work_date,
        source=source,
        created_by=created_by,
        import_batch_id=import_batch_id,
        is_active=True,
        **values,
    )
    db.add(row)
    return row, "replaced" if current is not None else "created"


def revert_override(db: Session, employee_id: int, work_date: date, by: Optional[str]) -> Optional[AttendanceOverride]:
    """Deactivates the active override (the day falls back to Matrix data).
    Returns it, or None when there was none. Does not commit."""
    current = get_active(db, employee_id, work_date)
    if current is not None:
        _deactivate(current, by)
    return current
