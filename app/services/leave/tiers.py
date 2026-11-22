"""Monthly credit by length of service (credit_mode MONTHLY_TIERED).

Each month an employee is credited days_per_month of the tier matching the
completed months of service at the first day of that month. A month credited
under a tier without carry forward lapses at the next month's credit run: a
LAPSE row (reference monthly_lapse YYYY-MM) removes what is left of that
month's credit after the leave dated in that month. Once a month has a LAPSE
row its lapse is kept in step with later leave changes (backdated, cancelled
or attendance-processed leave for that month)."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy.orm import Session

from app.models.leave import ENTRY_CREDIT, ENTRY_LAPSE, LeaveApplication, LeaveLedger, LeaveType

ZERO = Decimal("0")
REF_MONTHLY_CREDIT = "monthly_credit"
REF_MONTHLY_LAPSE = "monthly_lapse"
REF_APPLICATION = "leave_application"
REF_ATTENDANCE_MONTH = "attendance_month"


@dataclass(frozen=True)
class Tier:
    min_service_months: int
    days_per_month: Decimal
    carry_forward: bool
    carry_forward_limit: Optional[Decimal]
    next_min: Optional[int] = None


def _dec(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


def tiers_of(leave_type: LeaveType) -> list[Tier]:
    raw = sorted(leave_type.service_tiers or [], key=lambda t: int(t.get("min_service_months") or 0))
    mins = [int(t.get("min_service_months") or 0) for t in raw]
    return [
        Tier(
            min_service_months=mins[i], days_per_month=_dec(t.get("days_per_month")) or ZERO,
            carry_forward=bool(t.get("carry_forward")), carry_forward_limit=_dec(t.get("carry_forward_limit")),
            next_min=mins[i + 1] if i + 1 < len(mins) else None,
        )
        for i, t in enumerate(raw)
    ]


def tier_for(tiers: list[Tier], months: int) -> Optional[Tier]:
    match = None
    for tier in tiers:
        if tier.min_service_months <= months:
            match = tier
    return match


def service_months(joined: date, on: date) -> int:
    """Completed months of service on `on` (0 before the first month ends)."""
    months = (on.year - joined.year) * 12 + on.month - joined.month
    if on.day < joined.day:
        months -= 1
    return max(months, 0)


def employee_tier(leave_type: LeaveType, joined: Optional[date], month_first: date) -> Optional[Tier]:
    if joined is None:
        return None
    months = service_months(joined, month_first) if joined <= month_first else 0
    return tier_for(tiers_of(leave_type), months)


def month_ref(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def month_label(ref: str) -> str:
    year, month = ref.split("-")
    return date(int(year), int(month), 1).strftime("%b %Y")


def add_months(day: date, count: int) -> date:
    index = day.year * 12 + day.month - 1 + count
    return date(index // 12, index % 12 + 1, 1)


def fmt_days(value: Decimal) -> str:
    return format(Decimal(value).normalize(), "f")


def _span(months: int) -> str:
    if months % 12 == 0:
        years = months // 12
        return f"{years} year" + ("s" if years != 1 else "")
    return f"{months} month" + ("s" if months != 1 else "")


def tier_label(tier: Tier) -> str:
    """'< 1 year: 1/month, lapses' / '1 year+: 2/month, carries forward'."""
    if tier.min_service_months == 0:
        who = f"< {_span(tier.next_min)}" if tier.next_min else "All"
    elif tier.next_min:
        who = f"{_span(tier.min_service_months)} to < {_span(tier.next_min)}"
    else:
        who = f"{_span(tier.min_service_months)}+"
    rule = "lapses"
    if tier.carry_forward:
        rule = "carries forward"
        if tier.carry_forward_limit is not None:
            rule += f" (max {fmt_days(tier.carry_forward_limit)})"
    return f"{who}: {fmt_days(tier.days_per_month)}/month, {rule}"


@dataclass
class MonthUsage:
    leave_year: Optional[int] = None
    credit: Decimal = ZERO
    has_credit: bool = False
    used: Decimal = ZERO
    lapsed: Decimal = ZERO
    decided: bool = False

    @property
    def unused(self) -> Decimal:
        """What is left of the month's credit; leave in the month uses it first."""
        return max(self.credit - self.used, ZERO)

    @property
    def correction(self) -> Decimal:
        """Further days to lapse for a lapsed month (negative: days to restore)."""
        return self.unused - self.lapsed if self.decided else ZERO


Usage = dict[int, dict[str, MonthUsage]]


def month_usage(
    db: Session, leave_type_id: int, years: Iterable[int], employee_ids: Optional[Iterable[int]] = None
) -> Usage:
    """{employee_id: {"YYYY-MM": MonthUsage}} for the monthly credit, lapse
    and month-dated usage rows of the given leave years. Leave applications
    count in the month of their from_date; attendance debits in their month."""
    query = db.query(
        LeaveLedger.employee_id, LeaveLedger.leave_year, LeaveLedger.entry_type, LeaveLedger.days,
        LeaveLedger.reference_type, LeaveLedger.reference_id,
    ).filter(
        LeaveLedger.leave_type_id == leave_type_id,
        LeaveLedger.leave_year.in_(list(years)),
        LeaveLedger.reference_type.in_((REF_MONTHLY_CREDIT, REF_MONTHLY_LAPSE, REF_APPLICATION, REF_ATTENDANCE_MONTH)),
    )
    if employee_ids is not None:
        query = query.filter(LeaveLedger.employee_id.in_(list(employee_ids)))
    rows = query.all()
    app_ids = {
        int(r.reference_id) for r in rows
        if r.reference_type == REF_APPLICATION and (r.reference_id or "").isdigit()
    }
    app_month = {}
    if app_ids:
        apps = db.query(LeaveApplication.id, LeaveApplication.from_date).filter(LeaveApplication.id.in_(app_ids))
        app_month = {str(app_id): month_ref(from_date) for app_id, from_date in apps}

    usage: Usage = defaultdict(lambda: defaultdict(MonthUsage))
    for employee_id, leave_year, entry_type, days, ref_type, ref_id in rows:
        ref = app_month.get(ref_id) if ref_type == REF_APPLICATION else ref_id
        if not ref:
            continue
        month = usage[employee_id][ref]
        month.leave_year = month.leave_year or leave_year
        days = _dec(days) or ZERO
        if ref_type == REF_MONTHLY_CREDIT:
            if entry_type == ENTRY_CREDIT:
                month.credit += days
                month.has_credit = True
        elif ref_type == REF_MONTHLY_LAPSE:
            month.lapsed -= days
            month.decided = True
        else:
            month.used -= days
    return usage


def pending_corrections(
    db: Session, leave_type_id: int, year: int, employee_ids: Optional[Iterable[int]] = None
) -> dict[int, Decimal]:
    """Lapse changes not yet written for lapsed months of the year (leave for
    such a month recorded, cancelled or processed after it lapsed)."""
    result = {}
    for employee_id, months in month_usage(db, leave_type_id, [year], employee_ids).items():
        total = sum((m.correction for m in months.values()), ZERO)
        if total:
            result[employee_id] = total
    return result


def write_lapse(
    db: Session, employee_id: int, leave_type_id: int, ref: str, leave_year: int, days: Decimal, note: str,
    created_by: Optional[int] = None,
) -> LeaveLedger:
    row = LeaveLedger(
        employee_id=employee_id, leave_type_id=leave_type_id, leave_year=leave_year, entry_type=ENTRY_LAPSE,
        days=-days, reference_type=REF_MONTHLY_LAPSE, reference_id=ref, note=note, created_by=created_by,
    )
    db.add(row)
    return row


def resync_lapses(
    db: Session, leave_type: LeaveType, years: Iterable[int], employee_ids: Optional[Iterable[int]] = None,
    created_by: Optional[int] = None,
) -> tuple[int, Decimal]:
    """Writes the pending corrections as LAPSE rows; returns (rows, days lapsed)."""
    entries, days = 0, ZERO
    for employee_id, months in month_usage(db, leave_type.id, years, employee_ids).items():
        for ref, month in sorted(months.items()):
            delta = month.correction
            if not delta:
                continue
            verb = "lapsed" if delta > 0 else "restored"
            write_lapse(db, employee_id, leave_type.id, ref, month.leave_year, delta,
                        f"{leave_type.code} of {month_label(ref)}: {fmt_days(abs(delta))} more day(s) {verb} "
                        "after a later leave change for that month", created_by)
            entries += 1
            days += delta
    if entries:
        db.flush()
    return entries, days
