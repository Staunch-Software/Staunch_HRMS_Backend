"""Monthly attendance processing: resolve every day of a month for an
employee under the attendance policy, total the month, compute LOP and send
it to payroll.

resolve_month(db, employee, month, today=None) -> MonthResult   (writes nothing)
process_month(db, month, pay_group_id, user)    -> stores AttendanceMonthResult rows
send_to_payroll(db, month, pay_group_id, user, employee_ids=None)
    -> VariablePayInput (lop_days, present_days, paid_leave_days, source
       "Attendance") and this month's attendance ledger rows (LEAVE_DEBIT
       for leave corrections and sandwiched offs).

Day resolution, in order: not employed (before joining / after leaving) ->
LOP; weekly off / holiday (override WO/H, or the calendar) -> paid off,
unless sandwiched between two full-day leaves of sandwich-rule types;
approved leave (application wins over a LEAVE correction); attendance data
(STATUS codes, or punches: late mark from the entry time, missing punch
policy); no data on a working day -> absent (1 LOP). Approved work from
home on a working day counts as present (status WFH) with no punch needed
and no late check; a half-day WFH covers that half and the other half is
evaluated normally; WFH on an off day is ignored. Worked hours never cause
LOP. Each approved permission above the policy's half-day limit costs 0.5
day LOP, above the full-day limit 1 day. Late marks beyond the free ones are
grouped in blocks of late_block_size; every started block costs
late_deduction_days as straight LOP (month level): with 2 free, blocks of 3
and 0.5, lates 3-5 cost 0.5 day, 6-8 another 0.5. Days from
`today` on with no data are UPCOMING and carry no LOP (punches sync nightly).

Day counts are Decimal; the stored JSON holds them as strings ("0.5").
"""
from __future__ import annotations

import calendar as pycalendar
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional, Union

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.models.attendance import AttendanceDaily, AttendanceOverride
from app.models.attendance_month import MONTH_STATUS_PROCESSED, MONTH_STATUS_SENT, AttendanceMonthResult
from app.models.attendance_policy import POLICY_DEFAULTS, AttendancePolicy
from app.models.employee import Employee
from app.models.leave import (
    ENTRY_LEAVE_DEBIT,
    HALF_FIRST,
    HALF_FULL,
    LeaveLedger,
    LeaveType,
)
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.variable_pay import VariablePayInput
from app.services.attendance.devices import device_labels, label_of
from app.services.attendance.effective import SOURCE_OVERRIDE, EffectiveDay, effective_days
from app.services.attendance.overrides import KIND_STATUS, KIND_TIMES, STATUS_LEAVE
from app.services.leave import balance as balance_service
from app.services.leave.calendar import (
    KIND_HOLIDAY,
    KIND_WORK,
    day_kinds,
    policy_for,
    policy_for_location,
    saturday_off_gaps,
    saturday_off_warning,
)
from app.services.leave.queries import LeaveDayEntry, approved_leave_by_day, approved_permissions_by_day
from app.services.leave.wfh import approved_wfh_by_day
from app.services.payroll_engine.pay_groups import attendance_source_of, present_days_from_calendar
from app.services.payroll_settings import day_step, round_days
from app.services.variable_pay_inputs import find_input, locked_employee_ids

ZERO = Decimal("0")
HALF = Decimal("0.5")
ONE = Decimal("1")

REF_ATTENDANCE_MONTH = "attendance_month"
INPUT_SOURCE = "Attendance"
PROCESSED_SOURCES = ("BIOMETRIC", "MUSTER")
NEIGHBOUR_WINDOW_DAYS = 7

ST_PRESENT = "PRESENT"
ST_LATE = "LATE"
ST_HALF_DAY = "HALF_DAY"
ST_LEAVE = "LEAVE"
ST_OFF = "OFF"
ST_HOLIDAY = "HOLIDAY"
ST_ABSENT = "ABSENT"
ST_NOT_EMPLOYED = "NOT_EMPLOYED"
ST_UPCOMING = "UPCOMING"
ST_NO_DATA = "NO_DATA"
ST_WFH = "WFH"

LOP_ABSENT = "ABSENT"
LOP_PERMISSION = "PERMISSION"
LOP_MISSING_PUNCH = "MISSING_PUNCH"
LOP_NOT_EMPLOYED = "NOT_EMPLOYED"
LOP_UNPAID_LEAVE = "UNPAID_LEAVE"
LOP_NOTICE_LEAVE = "NOTICE_LEAVE"
LOP_NO_BALANCE = "NO_BALANCE"

FLAG_LATE = "LATE"
FLAG_LATE_EXCUSED = "LATE_EXCUSED"
FLAG_LATE_CHARGED = "LATE_CHARGED"
FLAG_LATE_BLOCK_START = "LATE_BLOCK_START"
FLAG_WFH = "WFH"
FLAG_PERMISSION_LOP = "PERMISSION_LOP"
FLAG_MISSING_OUT = "MISSING_OUT"
FLAG_MISSING_IN = "MISSING_IN"
FLAG_WORKED_ON_OFF = "WORKED_ON_OFF"
FLAG_CORRECTED = "CORRECTED"
FLAG_SANDWICH = "SANDWICH"
FLAG_AWAITING_UPLOAD = "AWAITING_UPLOAD"

LEAVE_FROM_APPLICATION = "APPLICATION"
LEAVE_FROM_OVERRIDE = "OVERRIDE"
LEAVE_FROM_SANDWICH = "SANDWICH"

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


class MonthlyError(Exception):
    def __init__(self, message: str, status_code: int = 400, detail: Optional[dict] = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.detail = detail


def parse_month(month: Union[str, date]) -> tuple[str, date, date]:
    """'YYYY-MM' (or any date in the month) -> ('YYYY-MM', first, last)."""
    if isinstance(month, date):
        first = month.replace(day=1)
    else:
        match = _MONTH_RE.match((month or "").strip())
        if not match or not 1 <= int(match.group(2)) <= 12:
            raise MonthlyError("month must be in YYYY-MM format.")
        first = date(int(match.group(1)), int(match.group(2)), 1)
    last = first.replace(day=pycalendar.monthrange(first.year, first.month)[1])
    return first.strftime("%Y-%m"), first, last


def num(value: Decimal) -> str:
    """Decimal day count as JSON text: 1, 0.5, 12.5."""
    return format(Decimal(value).normalize(), "f")


def _dec(value) -> Decimal:
    return balance_service.to_decimal(value)


def _hhmm(value: Optional[datetime]) -> Optional[str]:
    return value.strftime("%H:%M") if value is not None else None


@dataclass
class LeavePortion:
    code: str
    name: str
    days: Decimal
    half: str
    source: str  # APPLICATION | OVERRIDE | SANDWICH
    leave_type_id: Optional[int] = None
    paid_days: Decimal = ZERO
    lop_days: Decimal = ZERO
    # OVERRIDE / SANDWICH leave of a paid type is debited at send time.
    needs_debit: bool = False

    def to_json(self) -> dict:
        return {
            "code": self.code, "name": self.name, "days": num(self.days), "half": self.half,
            "source": self.source, "paid_days": num(self.paid_days), "lop_days": num(self.lop_days),
        }


@dataclass
class DayResult:
    date: date
    kind: str
    holiday_name: Optional[str] = None
    status: str = ST_ABSENT
    present: Decimal = ZERO
    off: Decimal = ZERO
    lop: Decimal = ZERO
    lop_reasons: dict = field(default_factory=dict)
    leave: list = field(default_factory=list)
    late: bool = False
    late_minutes: int = 0
    wfh: Decimal = ZERO
    wfh_half: Optional[str] = None
    upcoming: bool = False
    flags: list = field(default_factory=list)
    punch_in: Optional[datetime] = None
    punch_out: Optional[datetime] = None
    worked_minutes: Optional[int] = None
    permission_minutes: int = 0
    permissions: list = field(default_factory=list)  # minutes of each approved permission
    entry_device: Optional[str] = None
    exit_device: Optional[str] = None
    source: Optional[str] = None
    status_code: Optional[str] = None
    leave_type_code: Optional[str] = None
    override_id: Optional[int] = None
    override_source: Optional[str] = None
    override_reason: Optional[str] = None
    notes: list = field(default_factory=list)

    def add_lop(self, amount: Decimal, reason: str) -> None:
        if amount <= 0:
            return
        self.lop += amount
        self.lop_reasons[reason] = self.lop_reasons.get(reason, ZERO) + amount

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)

    def to_json(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "weekday": self.date.strftime("%a"),
            "kind": self.kind,
            "holiday_name": self.holiday_name,
            "status": self.status,
            "present": num(self.present),
            "off": num(self.off),
            "lop": num(self.lop),
            "lop_reasons": {k: num(v) for k, v in self.lop_reasons.items()},
            "leave": [p.to_json() for p in self.leave],
            "late": self.late,
            "late_minutes": self.late_minutes,
            "wfh": num(self.wfh),
            "wfh_half": self.wfh_half,
            "flags": list(self.flags),
            "in": _hhmm(self.punch_in),
            "out": _hhmm(self.punch_out),
            "worked_minutes": self.worked_minutes,
            "permission_minutes": self.permission_minutes,
            "permissions": list(self.permissions),
            "entry_device": self.entry_device,
            "exit_device": self.exit_device,
            "source": self.source,
            "status_code": self.status_code,
            "leave_type_code": self.leave_type_code,
            "override_id": self.override_id,
            "override_source": self.override_source,
            "override_reason": self.override_reason,
            "notes": list(self.notes),
        }


@dataclass
class MonthResult:
    employee_id: int
    month: str
    date_from: date
    date_to: date
    days: list
    totals: dict  # JSON-ready (numbers as strings)
    warnings: list
    lop_days: Decimal
    present_days: Decimal  # what payroll receives (see payroll_present_days)
    paid_leave_days: Decimal

    def days_json(self) -> list[dict]:
        return [d.to_json() for d in self.days]


# --- leave portions -----------------------------------------------------------------

def _in_notice(employee: Employee, leave_type: LeaveType, day: date) -> bool:
    notice = employee.notice_start_date
    return notice is not None and day >= notice and not leave_type.allowed_during_notice


def _add_application_leave(day: DayResult, entry: LeaveDayEntry, employee: Employee) -> None:
    """Approved application leave: already debited when approved, except
    unpaid types and notice-period days of types not allowed then (LOP)."""
    leave_type = entry.leave_type
    portion = LeavePortion(leave_type.code, leave_type.name, entry.fraction, entry.half, LEAVE_FROM_APPLICATION,
                           leave_type.id)
    if not leave_type.is_paid:
        portion.lop_days = entry.fraction
        day.add_lop(entry.fraction, LOP_UNPAID_LEAVE)
    elif _in_notice(employee, leave_type, day.date):
        portion.lop_days = entry.fraction
        day.add_lop(entry.fraction, LOP_NOTICE_LEAVE)
        day.notes.append(f"{leave_type.code} during the notice period is not deducted from the balance: LOP.")
    else:
        portion.paid_days = entry.fraction
    day.leave.append(portion)


def _add_debited_leave(
    day: DayResult, code: Optional[str], fraction: Decimal, half: str, source: str,
    types: dict[str, LeaveType], employee: Employee,
) -> None:
    """Leave from a LEAVE/HD correction or a sandwiched off: debited from the
    balance at send time (any shortfall becomes LOP when the month is totalled)."""
    code = (code or "").strip().upper()
    leave_type = types.get(code)
    if leave_type is None:
        day.leave.append(LeavePortion(code or "?", code or "Unknown", fraction, half, source, lop_days=fraction))
        day.add_lop(fraction, LOP_UNPAID_LEAVE)
        day.notes.append(f"Unknown leave type '{code}': treated as LOP.")
        return
    portion = LeavePortion(leave_type.code, leave_type.name, fraction, half, source, leave_type.id)
    if not leave_type.is_paid:
        portion.lop_days = fraction
        day.add_lop(fraction, LOP_UNPAID_LEAVE)
    elif _in_notice(employee, leave_type, day.date):
        portion.lop_days = fraction
        day.add_lop(fraction, LOP_NOTICE_LEAVE)
        day.notes.append(f"{leave_type.code} during the notice period is not deducted from the balance: LOP.")
    else:
        portion.paid_days = fraction
        portion.needs_debit = True
    day.leave.append(portion)


# --- off days and sandwiches ----------------------------------------------------------

def _override_off(eff: Optional[EffectiveDay]) -> bool:
    return eff is not None and eff.kind == KIND_STATUS and eff.status_code in ("WO", "H")


def _worked(eff: Optional[EffectiveDay]) -> bool:
    if eff is None:
        return False
    if eff.kind == KIND_TIMES:
        return eff.punch_in is not None or eff.punch_out is not None
    return eff.status_code in ("P", "OD", "HD")


def _full_sandwich_leave(
    day: date, effs: dict, leave_by_day: dict, types: dict[str, LeaveType]
) -> Optional[LeaveType]:
    """The sandwich-rule leave type when `day` is a full day of approved leave
    (application first, else a full-day LEAVE correction)."""
    entries = leave_by_day.get(day)
    if entries:
        full = [e for e in entries if e.half == HALF_FULL and e.fraction >= ONE]
        if full and full[0].leave_type.sandwich_rule:
            return full[0].leave_type
        return None
    eff = effs.get(day)
    if eff is not None and eff.kind == KIND_STATUS and eff.status_code == STATUS_LEAVE and (eff.half or HALF_FULL) == HALF_FULL:
        leave_type = types.get((eff.leave_type_code or "").strip().upper())
        if leave_type is not None and leave_type.sandwich_rule:
            return leave_type
    return None


def _sandwich_days(
    kinds: dict, effs: dict, leave_by_day: dict, types: dict[str, LeaveType], first: date, last: date
) -> dict[date, LeaveType]:
    """Calendar offs inside the month whose off-block sits between two
    full-day leaves of sandwich-rule types that come from different
    applications / corrections (offs inside one application are already
    counted by the application). The type is the one before the block."""
    ordered = sorted(kinds)
    is_off = {d: kinds[d][0] != KIND_WORK or _override_off(effs.get(d)) for d in ordered}
    result: dict[date, LeaveType] = {}
    i = 0
    while i < len(ordered):
        if not is_off[ordered[i]]:
            i += 1
            continue
        j = i
        while j + 1 < len(ordered) and is_off[ordered[j + 1]]:
            j += 1
        block = ordered[i:j + 1]
        i = j + 1
        if not any(first <= d <= last for d in block):
            continue
        before, after = block[0] - timedelta(days=1), block[-1] + timedelta(days=1)
        if before not in kinds or after not in kinds:
            continue
        before_type = _full_sandwich_leave(before, effs, leave_by_day, types)
        if before_type is None or _full_sandwich_leave(after, effs, leave_by_day, types) is None:
            continue
        for d in block:
            if (first <= d <= last and kinds[d][0] != KIND_WORK and not _worked(effs.get(d))
                    and not leave_by_day.get(d)):
                result[d] = before_type
    return result


# --- attendance on a working day ------------------------------------------------------

def _minutes_between(start: datetime, end: datetime) -> int:
    return int((end - start).total_seconds() // 60)


def _check_late(day: DayResult, punch_in: Optional[datetime], policy: AttendancePolicy, morning_on_leave: bool) -> None:
    """Late = entry after grace on a working day, unless the morning is on
    leave or the day's approved permission minutes cover the delay."""
    if punch_in is None or morning_on_leave or day.kind != KIND_WORK or day.present <= 0:
        return
    if punch_in.time() <= policy.grace_until:
        return
    late_by = max(_minutes_between(datetime.combine(punch_in.date(), policy.shift_start), punch_in), 0)
    day.late_minutes = late_by
    if day.permission_minutes >= late_by:
        day.flag(FLAG_LATE_EXCUSED)
        day.notes.append(f"Late by {late_by} min, covered by approved permission.")
        return
    day.late = True
    day.flag(FLAG_LATE)


def _evaluate_times(
    day: DayResult, punch_in: Optional[datetime], punch_out: Optional[datetime], portion: Decimal,
    leave_half: Optional[str], policy: AttendancePolicy, today: Optional[date] = None,
) -> None:
    """Punches make the day present; worked hours are shown, never LOP. An
    IN without OUT inside the late-upload hold is present with no
    missing-punch rule: the OUT may still arrive from an offline device."""
    morning_on_leave = leave_half == HALF_FIRST
    if punch_in is not None and punch_out is None and awaiting_upload(day.date, policy, today):
        day.present += portion
        day.flag(FLAG_AWAITING_UPLOAD)
        day.notes.append("Awaiting device upload: no OUT punch yet. The missing-punch rule applies after "
                         f"{late_upload_hold_days(policy)} day(s) if it still does not arrive.")
    elif punch_in is None or punch_out is None:
        missing = "OUT" if punch_out is None else "IN"
        day.flag(FLAG_MISSING_OUT if punch_out is None else FLAG_MISSING_IN)
        rule = (policy.missing_out_policy or "PRESENT").upper()
        if rule == "ABSENT":
            day.add_lop(portion, LOP_MISSING_PUNCH)
        elif rule == "HALF_DAY":
            lop = min(HALF, portion)
            day.add_lop(lop, LOP_MISSING_PUNCH)
            day.present += portion - lop
        else:
            day.present += portion
        day.notes.append(f"Missing {missing} punch ({rule} rule).")
    else:
        day.present += portion
    _check_late(day, punch_in, policy, morning_on_leave)


def late_upload_hold_days(policy: AttendancePolicy) -> int:
    value = getattr(policy, "late_upload_hold_days", None)
    return max(int(POLICY_DEFAULTS["late_upload_hold_days"] if value is None else value), 0)


def awaiting_upload(day: date, policy: AttendancePolicy, today: Optional[date]) -> bool:
    """True while `day` is younger than the policy's late-upload hold."""
    return today is not None and (today - day).days < late_upload_hold_days(policy)


def unsettled_from(last: date, policy: AttendancePolicy, today: Optional[date] = None) -> Optional[date]:
    """The first day of the month ending `last` that is still inside the
    late-upload hold (None when the whole month is settled)."""
    today = today or date.today()
    first_held = today - timedelta(days=late_upload_hold_days(policy) - 1)
    return first_held if first_held <= last and late_upload_hold_days(policy) > 0 else None


def _policy_minutes(policy: AttendancePolicy, name: str) -> int:
    value = getattr(policy, name, None)
    return int(POLICY_DEFAULTS[name] if value is None else value)


def permission_cost(minutes: int, policy: AttendancePolicy) -> Decimal:
    """LOP for one approved permission (policy section 5)."""
    if minutes > _policy_minutes(policy, "permission_full_day_above_minutes"):
        return ONE
    if minutes > _policy_minutes(policy, "permission_half_day_above_minutes"):
        return HALF
    return ZERO


def _apply_permission_lop(day: DayResult, policy: AttendancePolicy) -> None:
    """Each permission is judged on its own; the day cannot lose more than
    it was present."""
    costs = [(m, permission_cost(m, policy)) for m in day.permissions]
    lop = min(sum((c for _, c in costs), ZERO), day.present)
    if lop <= 0:
        return
    day.present -= lop
    day.add_lop(lop, LOP_PERMISSION)
    day.flag(FLAG_PERMISSION_LOP)
    charged = ", ".join(f"{m} min" for m, c in costs if c > 0)
    day.notes.append(f"Permission ({charged}): {num(lop)} day LOP.")


def _evaluate_attendance(
    day: DayResult, eff: Optional[EffectiveDay], portion: Decimal, leave_half: Optional[str],
    policy: AttendancePolicy, types: dict, employee: Employee, today: date,
) -> None:
    """The part of a working day not on leave (`portion` = 1 or 0.5)."""
    no_times = eff is not None and eff.kind == KIND_TIMES and eff.punch_in is None and eff.punch_out is None
    if eff is None or no_times:
        if day.date >= today:
            day.upcoming = True
            return
        day.add_lop(portion, LOP_ABSENT)
        return
    if eff.kind == KIND_TIMES:
        _evaluate_times(day, eff.punch_in, eff.punch_out, portion, leave_half, policy, today)
        return

    code = eff.status_code
    half = eff.half or HALF_FULL
    if code in ("P", "OD"):
        day.present += portion
    elif code == "A":
        if portion == ONE and half != HALF_FULL:
            day.present += HALF
            day.add_lop(HALF, LOP_ABSENT)
        else:
            day.add_lop(portion, LOP_ABSENT)
    elif code == "HD":
        if portion == ONE:
            day.present += HALF
            if eff.leave_type_code:
                _add_debited_leave(day, eff.leave_type_code, HALF, half, LEAVE_FROM_OVERRIDE, types, employee)
            else:
                day.add_lop(HALF, LOP_ABSENT)
        else:
            day.present += portion
    elif code == STATUS_LEAVE:
        # An application already covers part of the day and wins over the
        # correction: the rest is judged on the biometric punches.
        if eff.punch_in is None and eff.punch_out is None:
            day.add_lop(portion, LOP_ABSENT)
        else:
            _evaluate_times(day, eff.punch_in, eff.punch_out, portion, leave_half, policy, today)
    else:
        day.add_lop(portion, LOP_ABSENT)
        day.notes.append(f"Unrecognised status {code}: treated as absent.")


def _apply_wfh(
    day: DayResult, wfh_half: str, covered: Decimal, leave_half: Optional[str], eff: Optional[EffectiveDay],
) -> tuple[Decimal, Optional[str]]:
    """Approved WFH covers the part of a working day not on leave (only its
    half for a half-day request): present, no punch needed, no late check.
    Returns (portion still judged on attendance data, the half covered)."""
    remaining = ONE - min(covered, ONE)
    if remaining <= 0 or (remaining < ONE and wfh_half == leave_half):
        day.notes.append("WFH ignored: approved leave covers that part of the day.")
        return remaining, leave_half
    if remaining == ONE and wfh_half != HALF_FULL:
        wfh, covered_half = HALF, wfh_half
    else:
        wfh, covered_half = remaining, HALF_FULL
    day.wfh, day.wfh_half = wfh, wfh_half
    day.present += wfh
    day.flag(FLAG_WFH)
    remaining -= wfh
    label = {HALF_FIRST: " (first half)", HALF_FULL: ""}.get(wfh_half, " (second half)")
    if remaining <= 0 and eff is not None and eff.kind == KIND_STATUS and eff.status_code != STATUS_LEAVE:
        day.notes.append(f"Correction {eff.status_code} ignored: approved work from home covers this day.")
    elif remaining <= 0 and (eff is not None and (eff.punch_in is not None or eff.punch_out is not None)):
        day.notes.append(f"Work from home{label}: also punched, still counted as WFH.")
    else:
        day.notes.append(f"Work from home{label}.")
    return remaining, covered_half


def _final_status(day: DayResult) -> str:
    if day.wfh >= ONE:
        return ST_WFH
    if day.present >= ONE:
        return ST_LATE if day.late else ST_PRESENT
    if day.present > 0:
        return ST_HALF_DAY
    if day.leave:
        return ST_LEAVE
    if day.upcoming:
        return ST_UPCOMING
    return ST_ABSENT


def _resolve_day(
    d: date, employee: Employee, policy: AttendancePolicy, kinds: dict, effs: dict, leave_by_day: dict,
    permissions: dict, types: dict, sandwich: dict, today: date, no_punch_dates: frozenset = frozenset(),
    wfh_by_day: Optional[dict] = None,
) -> DayResult:
    kind, holiday = kinds[d]
    eff = effs.get(d)
    day = DayResult(date=d, kind=kind, holiday_name=holiday.name if holiday else None)
    day.permissions = list(permissions.get(d, []))
    day.permission_minutes = sum(day.permissions)
    if eff is not None:
        day.source = eff.source
        day.entry_device, day.exit_device = eff.entry_device, eff.exit_device
        day.punch_in, day.punch_out, day.worked_minutes = eff.punch_in, eff.punch_out, eff.worked_minutes
        day.status_code, day.leave_type_code = eff.status_code, eff.leave_type_code
        if eff.source == SOURCE_OVERRIDE:
            day.flag(FLAG_CORRECTED)
            day.override_id, day.override_source, day.override_reason = eff.override_id, eff.override_source, eff.reason

    joined, left = employee.date_of_joining, employee.date_of_leaving
    if (joined and d < joined) or (left and d > left):
        day.status = ST_NOT_EMPLOYED
        day.add_lop(ONE, LOP_NOT_EMPLOYED)
        return day

    entries = leave_by_day.get(d, [])
    wfh_half = (wfh_by_day or {}).get(d)
    off_override = _override_off(eff)
    if wfh_half and (off_override or kind != KIND_WORK):
        day.notes.append("WFH ignored: the day is a weekly off / holiday.")
    if off_override and kind == KIND_WORK:
        day.status = ST_HOLIDAY if eff.status_code == "H" else ST_OFF
        day.off = ONE
        return day

    if kind != KIND_WORK:
        if entries:
            for entry in entries:
                _add_application_leave(day, entry, employee)
            day.flag(FLAG_SANDWICH)
            day.status = ST_LEAVE
            return day
        if _worked(eff):
            day.flag(FLAG_WORKED_ON_OFF)
            day.notes.append("Worked on a weekly off / holiday (no compensatory off).")
        elif d in sandwich:
            _add_debited_leave(day, sandwich[d].code, ONE, HALF_FULL, LEAVE_FROM_SANDWICH, types, employee)
            day.flag(FLAG_SANDWICH)
            day.notes.append("Off day between two leaves (sandwich rule).")
            day.status = ST_LEAVE
            return day
        elif eff is not None and eff.kind == KIND_STATUS and eff.status_code in ("A", STATUS_LEAVE):
            label = "holiday" if kind == KIND_HOLIDAY else "weekly off"
            day.notes.append(f"Correction {eff.status_code} ignored: the day is a {label}.")
        holiday_marked = off_override and eff.status_code == "H"
        day.status = ST_HOLIDAY if kind == KIND_HOLIDAY or holiday_marked else ST_OFF
        day.off = ONE
        return day

    if eff is None and not entries and not wfh_half and d in no_punch_dates:
        day.status = ST_NO_DATA
        day.notes.append("Nobody punched on this day: not counted as LOP. Add it as a holiday, "
                         "choose it as the Saturday off, or sync attendance for this date.")
        return day

    covered = ZERO
    leave_half: Optional[str] = None
    if entries:
        for entry in entries:
            _add_application_leave(day, entry, employee)
            covered += entry.fraction
        leave_half = HALF_FULL if covered >= ONE else entries[0].half
        if eff is not None and eff.kind == KIND_STATUS and eff.status_code == STATUS_LEAVE:
            day.notes.append("Leave correction ignored: an approved leave application covers this day.")
    elif eff is not None and eff.kind == KIND_STATUS and eff.status_code == STATUS_LEAVE:
        half = eff.half or HALF_FULL
        covered = ONE if half == HALF_FULL else HALF
        leave_half = half
        _add_debited_leave(day, eff.leave_type_code, covered, half, LEAVE_FROM_OVERRIDE, types, employee)

    remaining = ONE - min(covered, ONE)
    if wfh_half:
        remaining, leave_half = _apply_wfh(day, wfh_half, covered, leave_half, eff)
    if remaining > 0:
        _evaluate_attendance(day, eff, remaining, leave_half, policy, types, employee, today)
    # Status shows attendance; a permission's cost shows as LOP only.
    day.status = _final_status(day)
    if remaining > 0:
        _apply_permission_lop(day, policy)
    return day


# --- balances and month totals -------------------------------------------------------

class _BalancePool:
    """Available days per paid leave type for one employee and leave year:
    the ledger balance minus pending applications, with this month's own
    attendance ledger rows added back (a re-process replaces them)."""

    def __init__(self, db: Session, employee_id: int, year: int, month_ref: str):
        self.db, self.employee_id, self.year, self.month_ref = db, employee_id, year, month_ref
        self._available: dict[int, Decimal] = {}

    def available(self, leave_type: LeaveType) -> Decimal:
        if leave_type.id not in self._available:
            balance = balance_service.balance(self.db, self.employee_id, leave_type.id, self.year)
            pending = balance_service.pending_days(self.db, self.employee_id, leave_type.id, self.year)
            own = self.db.query(func.coalesce(func.sum(LeaveLedger.days), 0)).filter(
                LeaveLedger.employee_id == self.employee_id,
                LeaveLedger.leave_type_id == leave_type.id,
                LeaveLedger.leave_year == self.year,
                LeaveLedger.reference_type == REF_ATTENDANCE_MONTH,
                LeaveLedger.reference_id == self.month_ref,
            ).scalar()
            self._available[leave_type.id] = balance - pending - _dec(own)
        return self._available[leave_type.id]

    def take(self, leave_type: LeaveType, days: Decimal) -> Decimal:
        available = self.available(leave_type)
        taken = days if leave_type.allow_negative_balance else min(max(available, ZERO), days)
        self._available[leave_type.id] = available - taken
        return taken


_DEBIT_LABELS = {
    LEAVE_FROM_OVERRIDE: "leave marked in attendance",
    LEAVE_FROM_SANDWICH: "sandwiched off days",
}


def _by_code(items: dict) -> dict:
    return {code: num(value) for code, value in sorted(items.items())}


def _month_totals(
    db: Session, employee: Employee, policy: AttendancePolicy, ref: str, first: date, last: date,
    days: list, types: dict, today: date,
) -> MonthResult:
    year = balance_service.leave_year_for(db, first)
    pool = _BalancePool(db, employee.id, year, ref)
    type_by_id = {t.id: t for t in types.values()}
    debit_amounts: dict[tuple, Decimal] = defaultdict(lambda: ZERO)

    for day in days:
        for portion in day.leave:
            if not portion.needs_debit:
                continue
            leave_type = type_by_id[portion.leave_type_id]
            taken = pool.take(leave_type, portion.days)
            short = portion.days - taken
            portion.paid_days = taken
            if short > 0:
                portion.lop_days += short
                day.add_lop(short, LOP_NO_BALANCE)
                day.notes.append(f"Not enough {leave_type.code} balance: {num(short)} day(s) LOP.")
            if taken > 0:
                debit_amounts[(ENTRY_LEAVE_DEBIT, portion.source, leave_type.id)] += taken

    late_days = [d for d in days if d.late]
    late_count = len(late_days)
    free_lates = int(policy.free_lates_per_month or 0)
    block_size = max(int(getattr(policy, "late_block_size", None) or POLICY_DEFAULTS["late_block_size"]), 1)
    chargeable = max(0, late_count - free_lates)
    late_blocks = -(-chargeable // block_size)
    per_block = _dec(policy.late_deduction_days)
    late_lop = Decimal(late_blocks) * per_block
    for position, day in enumerate(late_days[free_lates:]):
        index = free_lates + position + 1
        block = position // block_size + 1
        start = free_lates + (block - 1) * block_size + 1
        day.flag(FLAG_LATE_CHARGED)
        if position % block_size == 0:
            day.flag(FLAG_LATE_BLOCK_START)
            span = f"lates {start}-{start + block_size - 1}" if block_size > 1 else f"late {start}"
            day.notes.append(f"Late #{index} this month starts late block {block} ({span}): "
                             f"{num(per_block)} day LOP.")
        else:
            day.notes.append(f"Late #{index} this month: in late block {block} started by late #{start} "
                             "(no extra LOP).")

    lop_by_reason: dict[str, Decimal] = defaultdict(lambda: ZERO)
    paid_by_type: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for day in days:
        for reason, value in day.lop_reasons.items():
            lop_by_reason[reason] += value
        for portion in day.leave:
            if portion.paid_days > 0:
                paid_by_type[portion.code] += portion.paid_days

    step = day_step(db)
    present = round_days(sum((d.present for d in days), ZERO), step)
    wfh_days = sum((d.wfh for d in days), ZERO)
    paid_leave = round_days(sum(paid_by_type.values(), ZERO), step)
    total_lop = round_days(sum((d.lop for d in days), ZERO) + late_lop, step)
    calendar_days = len(days)
    payroll_present = max(Decimal(calendar_days) - total_lop, ZERO)

    debits, debit_groups = [], defaultdict(dict)
    for (entry_type, source, type_id), amount in sorted(debit_amounts.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2])):
        code = type_by_id[type_id].code
        debit_groups[source][code] = amount
        debits.append({
            "entry_type": entry_type, "source": source, "leave_type_id": type_id, "code": code,
            "days": num(amount), "leave_year": year, "note": f"Attendance {ref}: {_DEBIT_LABELS[source]}",
        })

    def count(predicate) -> int:
        return sum(1 for d in days if predicate(d))

    upcoming = count(lambda d: d.upcoming)
    awaiting = count(lambda d: FLAG_AWAITING_UPLOAD in d.flags)
    missing = count(lambda d: FLAG_MISSING_OUT in d.flags or FLAG_MISSING_IN in d.flags)
    with_data = count(lambda d: d.source is not None)
    totals = {
        "calendar_days": calendar_days,
        "working_days": count(lambda d: d.kind == KIND_WORK and d.status not in (ST_NOT_EMPLOYED, ST_OFF, ST_HOLIDAY)),
        "present_days": num(present),
        "wfh_days": num(wfh_days),
        "paid_leave_days": num(paid_leave),
        "paid_leave_by_type": _by_code(paid_by_type),
        "weekly_offs": count(lambda d: d.status == ST_OFF),
        "holidays": count(lambda d: d.status == ST_HOLIDAY),
        "off_days": count(lambda d: d.status in (ST_OFF, ST_HOLIDAY)),
        "late_count": late_count,
        "free_lates": free_lates,
        "chargeable_lates": chargeable,
        "late_block_size": block_size,
        "late_blocks": late_blocks,
        "late_deduction_days": num(late_lop),
        "late_lop": num(late_lop),
        "permission_lop": num(lop_by_reason[LOP_PERMISSION]),
        "permission_lop_days": count(lambda d: FLAG_PERMISSION_LOP in d.flags),
        "absent_lop": num(lop_by_reason[LOP_ABSENT]),
        "missing_punch_lop": num(lop_by_reason[LOP_MISSING_PUNCH]),
        "not_employed_days": num(lop_by_reason[LOP_NOT_EMPLOYED]),
        "unpaid_leave_lop": num(lop_by_reason[LOP_UNPAID_LEAVE]),
        "notice_leave_lop": num(lop_by_reason[LOP_NOTICE_LEAVE]),
        "no_balance_lop": num(lop_by_reason[LOP_NO_BALANCE]),
        "override_leave_debits": _by_code(debit_groups[LEAVE_FROM_OVERRIDE]),
        "sandwich_debits": _by_code(debit_groups[LEAVE_FROM_SANDWICH]),
        "sandwich_days": count(lambda d: any(p.source == LEAVE_FROM_SANDWICH for p in d.leave)),
        "total_lop": num(total_lop),
        "payroll_present_days": num(payroll_present),
        "missing_punch_days": missing,
        "worked_on_off_days": count(lambda d: FLAG_WORKED_ON_OFF in d.flags),
        "corrected_days": count(lambda d: FLAG_CORRECTED in d.flags),
        "late_excused_days": count(lambda d: FLAG_LATE_EXCUSED in d.flags),
        "upcoming_days": upcoming,
        "awaiting_upload_days": awaiting,
        "days_with_data": with_data,
        "leave_year": year,
        "debits": debits,
    }

    warnings = []
    if with_data == 0 and any(d.status != ST_NOT_EMPLOYED for d in days):
        warnings.append(f"No attendance data for {ref}: working days without leave are LOP.")
    if missing:
        warnings.append(f"{missing} day(s) with a missing punch.")
    if awaiting:
        warnings.append(f"{awaiting} day(s) awaiting device upload (IN without OUT in the last "
                        f"{late_upload_hold_days(policy)} day(s)): re-process once the OUT punches arrive.")
    if upcoming:
        warnings.append(f"Month in progress: {upcoming} day(s) from {today:%d %b %Y} on are not evaluated yet.")
    if any(any(n.startswith("Unknown leave type") for n in d.notes) for d in days):
        warnings.append("Unknown leave codes in attendance corrections were treated as LOP.")

    return MonthResult(
        employee_id=employee.id, month=ref, date_from=first, date_to=last, days=days, totals=totals,
        warnings=warnings, lop_days=total_lop, present_days=payroll_present, paid_leave_days=paid_leave,
    )


def no_punch_dates(db: Session, first: date, last: date, today: date) -> frozenset:
    """Past dates in the range on which nobody punched and no correction exists.
    Such a day is an unlisted holiday or a gap in the sync, never an absence."""
    end = min(last, today - timedelta(days=1))
    if end < first:
        return frozenset()
    seen = {r[0] for r in db.query(AttendanceDaily.work_date).filter(
        AttendanceDaily.work_date >= first, AttendanceDaily.work_date <= end).distinct()}
    seen |= {r[0] for r in db.query(AttendanceOverride.work_date).filter(
        AttendanceOverride.is_active.is_(True),
        AttendanceOverride.work_date >= first, AttendanceOverride.work_date <= end).distinct()}
    return frozenset(first + timedelta(days=i) for i in range((end - first).days + 1)
                     if first + timedelta(days=i) not in seen)


def resolve_month(db: Session, employee: Employee, month: Union[str, date], today: Optional[date] = None) -> MonthResult:
    """Resolves every day of the month for one employee. Writes nothing."""
    ref, first, last = parse_month(month)
    today = today or date.today()
    gaps_no_punch = no_punch_dates(db, first, last, today)
    policy = policy_for(db, employee)
    window_from = first - timedelta(days=NEIGHBOUR_WINDOW_DAYS)
    window_to = last + timedelta(days=NEIGHBOUR_WINDOW_DAYS)
    kinds = day_kinds(db, employee, window_from, window_to, policy)
    effs = {d: e for (_, d), e in effective_days(db, [employee.id], window_from, window_to).items()}
    leave_by_day = approved_leave_by_day(db, employee.id, window_from, window_to)
    permissions = approved_permissions_by_day(db, employee.id, first, last)
    wfh_by_day = approved_wfh_by_day(db, employee.id, first, last)
    labels = device_labels(db)
    types = {t.code.strip().upper(): t for t in db.query(LeaveType).all()}
    sandwich = _sandwich_days(kinds, effs, leave_by_day, types, first, last)

    days = []
    d = first
    while d <= last:
        day = _resolve_day(d, employee, policy, kinds, effs, leave_by_day, permissions, types, sandwich, today,
                           gaps_no_punch, wfh_by_day)
        day.entry_device, day.exit_device = label_of(labels, day.entry_device), label_of(labels, day.exit_device)
        days.append(day)
        d += timedelta(days=1)
    result = _month_totals(db, employee, policy, ref, first, last, days, types, today)
    gaps = saturday_off_gaps(db, policy, employee.work_location_id, first, last)
    result.warnings[:0] = [saturday_off_warning(month, assumed) for month, assumed in gaps]
    result.totals["saturday_off_assumed"] = bool(gaps)
    no_data = [day.date for day in days if day.status == ST_NO_DATA]
    result.totals["no_punch_days"] = len(no_data)
    result.totals["no_punch_dates"] = [d.isoformat() for d in no_data]
    if no_data:
        result.warnings.append(no_punch_warning(no_data))
    return result


def no_punch_warning(dates: Iterable[date]) -> str:
    listed = ", ".join(f"{d:%d %b}" for d in sorted(dates))
    return (f"Nobody punched on {listed}: these days are not counted as LOP. "
            "Add them as holidays (or choose one as the Saturday off), or sync attendance for them.")


def saturday_off_warnings(db: Session, employees: Iterable[Employee], month: Union[str, date]) -> list[str]:
    """Live check for the employees' locations: months whose Saturday off HR
    has not chosen yet (one message per month)."""
    _, first, last = parse_month(month)
    messages: list[str] = []
    for location_id in sorted({e.work_location_id for e in employees}, key=lambda x: (x is not None, x or 0)):
        policy = policy_for_location(db, location_id)
        for gap_month, assumed in saturday_off_gaps(db, policy, location_id, first, last):
            message = saturday_off_warning(gap_month, assumed)
            if message not in messages:
                messages.append(message)
    return messages


# --- pay-group scope, processing and send to payroll -----------------------------------

def _snapshot(totals: dict) -> dict:
    """The figures payroll receives; a re-process that changes any of them
    takes a SENT result back to PROCESSED."""
    return {
        "total_lop": totals.get("total_lop"),
        "payroll_present_days": totals.get("payroll_present_days"),
        "present_days": totals.get("present_days"),
        "paid_leave_days": totals.get("paid_leave_days"),
        "debits": [
            {k: d.get(k) for k in ("entry_type", "source", "code", "days", "leave_year")}
            for d in totals.get("debits") or []
        ],
    }


def changed_since_sent(row: AttendanceMonthResult) -> bool:
    return row.sent_at is not None and row.status != MONTH_STATUS_SENT


def _active_in_month(employee: Employee, first: date, last: date) -> bool:
    if employee.date_of_joining and employee.date_of_joining > last:
        return False
    if employee.date_of_leaving and employee.date_of_leaving < first:
        return False
    if (employee.status or "").strip().lower() == "active":
        return True
    return employee.date_of_leaving is not None and employee.date_of_leaving >= first


def group_scope(db: Session, group: PayGroup, first: date, last: date) -> list[Employee]:
    """Employees whose latest pay profile overlapping the month is in `group`
    and who were employed during the month."""
    profiles = (
        db.query(EmployeePayProfile)
        .filter(
            EmployeePayProfile.effective_from <= last,
            or_(EmployeePayProfile.effective_to.is_(None), EmployeePayProfile.effective_to >= first),
        )
        .order_by(EmployeePayProfile.effective_from.desc(), EmployeePayProfile.id.desc())
    )
    latest: dict[int, EmployeePayProfile] = {}
    for profile in profiles:
        latest.setdefault(profile.employee_id, profile)
    ids = [employee_id for employee_id, p in latest.items() if p.pay_group_id == group.id]
    if not ids:
        return []
    employees = db.query(Employee).filter(Employee.id.in_(ids)).order_by(Employee.employee_code).all()
    return [e for e in employees if _active_in_month(e, first, last)]


def processable_group(db: Session, pay_group_id: int) -> PayGroup:
    group = db.get(PayGroup, pay_group_id)
    if group is None:
        raise MonthlyError("Pay group not found.", 404)
    source = attendance_source_of(group)
    if source not in PROCESSED_SOURCES:
        raise MonthlyError(
            f"Pay group {group.code} takes LOP from monthly inputs (attendance source {source}); "
            "attendance is not processed for it.", 400,
        )
    return group


def _store(row: AttendanceMonthResult, group: PayGroup, result: MonthResult, now: datetime, by: Optional[str]) -> None:
    row.pay_group_id = group.id
    row.totals = result.totals
    row.days = result.days_json()
    row.warnings = list(result.warnings)
    row.lop_days = result.lop_days
    row.present_days = _dec(result.totals["present_days"])
    row.paid_leave_days = result.paid_leave_days
    row.processed_at = now
    row.processed_by = by
    unchanged = row.sent_at is not None and row.sent_snapshot == _snapshot(result.totals)
    row.status = MONTH_STATUS_SENT if unchanged else MONTH_STATUS_PROCESSED


def process_month(
    db: Session, month: Union[str, date], pay_group_id: int, user=None, today: Optional[date] = None,
    employee_ids: Optional[Iterable[int]] = None,
) -> dict:
    """Resolves the month for every employee in the pay group (or only
    `employee_ids` of them) and stores the results (no commit). On a full run,
    results of employees no longer in the group that were never sent are
    removed."""
    ref, first, last = parse_month(month)
    group = processable_group(db, pay_group_id)
    employees = group_scope(db, group, first, last)
    if employee_ids is not None:
        wanted = set(employee_ids)
        employees = [e for e in employees if e.id in wanted]
    ids = [e.id for e in employees]
    existing = {}
    if ids:
        existing = {
            r.employee_id: r for r in db.query(AttendanceMonthResult).filter(
                AttendanceMonthResult.month == ref, AttendanceMonthResult.employee_id.in_(ids)
            )
        }
    now = datetime.now(timezone.utc)
    by = getattr(user, "username", None)
    created = 0
    for employee in employees:
        result = resolve_month(db, employee, ref, today)
        row = existing.get(employee.id)
        if row is None:
            row = AttendanceMonthResult(employee_id=employee.id, month=ref, status=MONTH_STATUS_PROCESSED)
            db.add(row)
            created += 1
        _store(row, group, result, now, by)

    removed = 0
    if employee_ids is None:
        stale = db.query(AttendanceMonthResult).filter(
            AttendanceMonthResult.month == ref, AttendanceMonthResult.pay_group_id == group.id,
            AttendanceMonthResult.sent_at.is_(None),
        )
        if ids:
            stale = stale.filter(AttendanceMonthResult.employee_id.notin_(ids))
        for row in stale.all():
            db.delete(row)
            removed += 1
    db.flush()
    return {"month": ref, "pay_group_id": group.id, "processed": len(employees), "created": created,
            "removed": removed}


def send_to_payroll(
    db: Session, month: Union[str, date], pay_group_id: int, user=None, employee_ids: Optional[Iterable[int]] = None
) -> dict:
    """Writes the processed LOP into the period's VariablePayInput rows and
    replaces this month's attendance ledger rows (no commit). 409 when any
    target input is Locked."""
    ref, first, last = parse_month(month)
    group = processable_group(db, pay_group_id)
    query = db.query(AttendanceMonthResult).filter(
        AttendanceMonthResult.month == ref, AttendanceMonthResult.pay_group_id == group.id
    )
    if employee_ids is not None:
        ids = list(set(employee_ids))
        if not ids:
            raise MonthlyError("No employees selected.")
        query = query.filter(AttendanceMonthResult.employee_id.in_(ids))
    rows = query.order_by(AttendanceMonthResult.employee_id).all()
    if not rows:
        raise MonthlyError(f"Nothing to send: process {ref} for {group.code} first.")

    locked = locked_employee_ids(db, [r.employee_id for r in rows], first, last)
    if locked:
        codes = sorted(code for (code,) in db.query(Employee.employee_code).filter(Employee.id.in_(locked)))
        message = (f"Monthly inputs for {ref} are Locked for {len(codes)} employee(s) ({', '.join(codes)}): "
                   "a payroll run has already used them, so attendance cannot replace them. Nothing was sent.")
        raise MonthlyError(message, 409, {"message": message, "locked_employees": codes})

    now = datetime.now(timezone.utc)
    by = getattr(user, "username", None)
    monthly_salaried = present_days_from_calendar(group)
    created = updated = ledger_rows = 0
    total_lop = ZERO
    for row in rows:
        totals = row.totals or {}
        lop = _dec(totals.get("total_lop", row.lop_days))
        present = _dec(totals.get("payroll_present_days" if monthly_salaried else "present_days"))
        vi = find_input(db, row.employee_id, first, last)
        if vi is None:
            vi = VariablePayInput(employee_id=row.employee_id, period_start=first, period_end=last, status="Draft",
                                  overtime_hours=0, ad_hoc_earnings=[], ad_hoc_deductions=[])
            db.add(vi)
            created += 1
        else:
            updated += 1
        vi.lop_days = lop
        vi.present_days = present
        vi.paid_leave_days = _dec(totals.get("paid_leave_days"))
        vi.source = INPUT_SOURCE

        db.query(LeaveLedger).filter(
            LeaveLedger.employee_id == row.employee_id,
            LeaveLedger.reference_type == REF_ATTENDANCE_MONTH,
            LeaveLedger.reference_id == ref,
        ).delete(synchronize_session=False)
        for debit in totals.get("debits") or []:
            db.add(LeaveLedger(
                employee_id=row.employee_id, leave_type_id=debit["leave_type_id"], leave_year=debit["leave_year"],
                entry_type=debit["entry_type"], days=-_dec(debit["days"]), reference_type=REF_ATTENDANCE_MONTH,
                reference_id=ref, note=debit.get("note"), created_by=getattr(user, "id", None),
            ))
            ledger_rows += 1

        row.status = MONTH_STATUS_SENT
        row.sent_at = now
        row.sent_by = by
        row.sent_snapshot = _snapshot(totals)
        total_lop += lop
    db.flush()
    return {"month": ref, "pay_group_id": group.id, "sent": len(rows), "total_lop": total_lop,
            "inputs_created": created, "inputs_updated": updated, "ledger_rows": ledger_rows}


def unmatched_punch_codes(db: Session, first: date, last: date) -> list[str]:
    """Matrix employee codes with daily rows in the range that match no employee."""
    from app.services.attendance.sync import employee_code_map

    known = set(employee_code_map(db))
    rows = (
        db.query(AttendanceDaily.employee_code)
        .filter(AttendanceDaily.work_date >= first, AttendanceDaily.work_date <= last)
        .distinct()
    )
    return sorted(code for (code,) in rows if code not in known)
