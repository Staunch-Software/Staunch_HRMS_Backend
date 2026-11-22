"""Leave balances from the ledger, yearly credits, carry forward and the
debits/reversals written when leave is approved or cancelled."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.leave import (
    CREDIT_MONTHLY_ACCRUAL,
    CREDIT_MONTHLY_TIERED,
    ENTRY_ADJUSTMENT,
    ENTRY_CARRY_FORWARD,
    ENTRY_CREDIT,
    ENTRY_LAPSE,
    ENTRY_LATE_DEBIT,
    ENTRY_LEAVE_DEBIT,
    ENTRY_REVERSAL,
    PENDING_STATUSES,
    LeaveApplication,
    LeaveLedger,
    LeaveType,
)
from app.services.leave import tiers
from app.services.leave.calendar import default_policy, leave_year_bounds, leave_year_of

ZERO = Decimal("0")
REF_APPLICATION = "leave_application"
REF_YEARLY_CREDIT = "yearly_credit"
REF_ACCRUAL = "accrual"
REF_CARRY_FORWARD = "carry_forward"
REF_OPENING_CARRY_FORWARD = "opening_carry_forward"
# Offsets the carry forward of a year when it was reset to 0.
REF_CARRY_FORWARD_RESET = "carry_forward_reset"


def to_decimal(value) -> Decimal:
    if value is None:
        return ZERO
    return value if isinstance(value, Decimal) else Decimal(str(value))


def round_half_day(value: Decimal) -> Decimal:
    return (value * 2).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / 2


def leave_year_for(db: Session, day: date) -> int:
    """Leave years are company-wide: they follow the default policy."""
    return leave_year_of(default_policy(db), day)


def year_bounds(db: Session, year: int) -> tuple[date, date]:
    return leave_year_bounds(default_policy(db), year)


def is_tiered(leave_type: Optional[LeaveType]) -> bool:
    return leave_type is not None and leave_type.credit_mode == CREDIT_MONTHLY_TIERED


def balance(db: Session, employee_id: int, leave_type_id: int, year: int) -> Decimal:
    """Ledger sum for the leave year. For a MONTHLY_TIERED type the LAPSE
    rows make this the usable balance: an employee in a tier without carry
    forward is left with the current month's credit only."""
    total = to_decimal(
        db.query(func.coalesce(func.sum(LeaveLedger.days), 0))
        .filter(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.leave_year == year,
        )
        .scalar()
    )
    if is_tiered(db.get(LeaveType, leave_type_id)):
        total -= tiers.pending_corrections(db, leave_type_id, year, [employee_id]).get(employee_id, ZERO)
    return total


def pending_days(
    db: Session, employee_id: int, leave_type_id: int, year: int, exclude_id: Optional[int] = None
) -> Decimal:
    start, end = year_bounds(db, year)
    query = db.query(func.coalesce(func.sum(LeaveApplication.days), 0)).filter(
        LeaveApplication.employee_id == employee_id,
        LeaveApplication.leave_type_id == leave_type_id,
        LeaveApplication.status.in_(PENDING_STATUSES),
        LeaveApplication.from_date >= start,
        LeaveApplication.from_date <= end,
    )
    if exclude_id is not None:
        query = query.filter(LeaveApplication.id != exclude_id)
    return to_decimal(query.scalar())


def balance_summaries(
    db: Session, year: int, employee_ids: Optional[Iterable[int]] = None
) -> dict[tuple[int, int], dict]:
    """{(employee_id, leave_type_id): summary} for every pair that has
    ledger rows or pending applications in the year."""
    ids = list(employee_ids) if employee_ids is not None else None
    ledger = db.query(
        LeaveLedger.employee_id, LeaveLedger.leave_type_id, LeaveLedger.entry_type, func.sum(LeaveLedger.days)
    ).filter(LeaveLedger.leave_year == year)
    if ids is not None:
        ledger = ledger.filter(LeaveLedger.employee_id.in_(ids))
    sums: dict[tuple[int, int], dict[str, Decimal]] = defaultdict(lambda: defaultdict(lambda: ZERO))
    for employee_id, leave_type_id, entry_type, total in ledger.group_by(
        LeaveLedger.employee_id, LeaveLedger.leave_type_id, LeaveLedger.entry_type
    ):
        sums[(employee_id, leave_type_id)][entry_type] += to_decimal(total)
    for leave_type in db.query(LeaveType).filter(LeaveType.credit_mode == CREDIT_MONTHLY_TIERED):
        for employee_id, delta in tiers.pending_corrections(db, leave_type.id, year, ids).items():
            sums[(employee_id, leave_type.id)][ENTRY_LAPSE] -= delta

    start, end = year_bounds(db, year)
    pending = db.query(
        LeaveApplication.employee_id, LeaveApplication.leave_type_id, func.sum(LeaveApplication.days)
    ).filter(
        LeaveApplication.status.in_(PENDING_STATUSES),
        LeaveApplication.from_date >= start,
        LeaveApplication.from_date <= end,
    )
    if ids is not None:
        pending = pending.filter(LeaveApplication.employee_id.in_(ids))
    pending_by_key = {
        (e, t): to_decimal(total)
        for e, t, total in pending.group_by(LeaveApplication.employee_id, LeaveApplication.leave_type_id)
    }

    result = {}
    for key in set(sums) | set(pending_by_key):
        entries = sums.get(key, {})
        total = sum(entries.values(), ZERO)
        pend = pending_by_key.get(key, ZERO)
        result[key] = summary_dict(entries, total, pend)
    return result


def summary_dict(entries: dict, total: Decimal, pending: Decimal) -> dict:
    return {
        "carried_forward": entries.get(ENTRY_CARRY_FORWARD, ZERO),
        "credited": entries.get(ENTRY_CREDIT, ZERO),
        "adjusted": entries.get(ENTRY_ADJUSTMENT, ZERO),
        "used": -(entries.get(ENTRY_LEAVE_DEBIT, ZERO) + entries.get(ENTRY_REVERSAL, ZERO)),
        "late_deducted": -entries.get(ENTRY_LATE_DEBIT, ZERO),
        "lapsed": -entries.get(ENTRY_LAPSE, ZERO),
        "balance": total,
        "pending": pending,
        "available": total - pending,
    }


def selected_month(db: Session, year: int, month: Optional[int] = None, today: Optional[date] = None) -> date:
    """First day of calendar month `month` within leave year `year`. Without
    a month: today's month when today is in the year, else the year's last
    (past year) or first (future year) month."""
    months = [first for first, _ in _months_in_year(db, year)]
    if month is not None:
        for first in months:
            if first.month == month:
                return first
        raise ValueError(f"Month {month} is not in leave year {year}.")
    today = today or date.today()
    current = today.replace(day=1)
    if current in months:
        return current
    return months[-1] if current > months[-1] else months[0]


def month_summaries(
    db: Session, year: int, first: date, leave_types: Iterable[LeaveType], employees: Iterable[Employee],
    summaries: dict,
) -> dict[tuple[int, int], dict]:
    """{(employee_id, leave_type_id): month fields} for the month starting
    `first`. month_credited is that month's own credit (monthly credit or
    accrual; the yearly credit counts in the leave year's first month),
    month_used the leave dated in the month, month_pending pending leave
    starting in it. month_available is what is usable for leave in the
    month: for a MONTHLY_TIERED type what is left of the month's credit (a
    month not credited yet counts its tier's credit), capped at the year's
    available once credited, as available_on(); for other types the year's
    available."""
    leave_types, employees = list(leave_types), list(employees)
    ids = [e.id for e in employees]
    ref = tiers.month_ref(first)
    last = tiers.add_months(first, 1) - timedelta(days=1)
    year_start, _ = year_bounds(db, year)
    if not ids or not leave_types:
        return {}

    credit_refs = [(REF_ACCRUAL, ref), (tiers.REF_MONTHLY_CREDIT, ref)]
    if first == year_start.replace(day=1):
        credit_refs.append((REF_YEARLY_CREDIT, str(year)))
    credited: dict[tuple[int, int], Decimal] = defaultdict(lambda: ZERO)
    rows = db.query(LeaveLedger.employee_id, LeaveLedger.leave_type_id, LeaveLedger.reference_type,
                    LeaveLedger.reference_id, LeaveLedger.days).filter(
        LeaveLedger.leave_year == year, LeaveLedger.entry_type == ENTRY_CREDIT, LeaveLedger.employee_id.in_(ids),
        LeaveLedger.reference_type.in_([r for r, _ in credit_refs]),
    )
    for employee_id, leave_type_id, ref_type, ref_id, days in rows:
        if (ref_type, ref_id) in credit_refs:
            credited[(employee_id, leave_type_id)] += to_decimal(days)

    pending_rows = db.query(
        LeaveApplication.employee_id, LeaveApplication.leave_type_id, func.sum(LeaveApplication.days)
    ).filter(
        LeaveApplication.status.in_(PENDING_STATUSES), LeaveApplication.employee_id.in_(ids),
        LeaveApplication.from_date >= first, LeaveApplication.from_date <= last,
    ).group_by(LeaveApplication.employee_id, LeaveApplication.leave_type_id)
    pending = {(e, t): to_decimal(total) for e, t, total in pending_rows}

    result = {}
    for leave_type in leave_types:
        usage = tiers.month_usage(db, leave_type.id, [year], ids)
        for employee in employees:
            key = (employee.id, leave_type.id)
            month = usage.get(employee.id, {}).get(ref)
            used = month.used if month else ZERO
            pend = pending.get(key, ZERO)
            year_available = (summaries.get(key) or empty_summary())["available"]
            available = year_available
            joined = employee.date_of_joining
            if is_tiered(leave_type) and joined is not None:
                tier = tiers.employee_tier(leave_type, joined, first)
                has_credit = month is not None and month.has_credit
                eligible = joined <= last and not (joined >= first and joined.day > (leave_type.joining_cutoff_day or 15))
                expected = month.credit if has_credit else (
                    tier.days_per_month if tier is not None and eligible else ZERO)
                left = expected - used - pend
                available = min(year_available, left) if has_credit else left
                if tier is None or tier.carry_forward:
                    available = max(available, ZERO)
            result[key] = {
                "month": ref, "month_credited": credited.get(key, ZERO), "month_used": used,
                "month_pending": pend, "month_available": available,
            }
    return result


def empty_summary() -> dict:
    return summary_dict({}, ZERO, ZERO)


def _has_entry(db: Session, employee_id: int, leave_type_id: int, entry_type: str, ref_type: str, ref_id: str) -> bool:
    return (
        db.query(LeaveLedger.id)
        .filter(
            LeaveLedger.employee_id == employee_id,
            LeaveLedger.leave_type_id == leave_type_id,
            LeaveLedger.entry_type == entry_type,
            LeaveLedger.reference_type == ref_type,
            LeaveLedger.reference_id == ref_id,
        )
        .first()
        is not None
    )


def _months_in_year(db: Session, year: int) -> list[tuple[date, date]]:
    start, _ = year_bounds(db, year)
    months = []
    y, m = start.year, start.month
    for _ in range(12):
        first = date(y, m, 1)
        ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
        months.append((first, date.fromordinal(date(ny, nm, 1).toordinal() - 1)))
        y, m = ny, nm
    return months


def _eligible_employees(db: Session, year: int, employee_ids: Optional[Iterable[int]]) -> list[Employee]:
    start, end = year_bounds(db, year)
    query = db.query(Employee).filter(Employee.status == "Active")
    if employee_ids is not None:
        query = query.filter(Employee.id.in_(list(employee_ids)))
    employees = []
    for employee in query.order_by(Employee.employee_code):
        if employee.date_of_joining and employee.date_of_joining > end:
            continue
        if employee.date_of_leaving and employee.date_of_leaving < start:
            continue
        employees.append(employee)
    return employees


def credit_year(
    db: Session,
    year: int,
    leave_type_ids: Optional[Iterable[int]] = None,
    as_of: Optional[date] = None,
    employee_ids: Optional[Iterable[int]] = None,
    created_by: Optional[int] = None,
) -> dict:
    """Credits active paid leave types for every active employee. Safe to
    re-run: each credit carries a reference (yearly_credit YYYY, accrual
    YYYY-MM, carry_forward YYYY) and is written at most once.

    YEARLY_UPFRONT credits the full entitlement, pro-rated by remaining months
    (joining month included, rounded to 0.5) for mid-year joiners when the
    type says so. MONTHLY_ACCRUAL credits entitlement/12 for every month of
    the leave year that has started by `as_of` (default today) and that the
    employee had joined by. Carry forward takes the previous year's positive
    balance, capped at carry_forward_limit, at the first run for the year.
    MONTHLY_TIERED types are credited month by month (credit_catch_up)."""
    as_of = as_of or date.today()
    start, end = year_bounds(db, year)
    types_query = db.query(LeaveType).filter(LeaveType.is_active.is_(True), LeaveType.is_paid.is_(True))
    if leave_type_ids is not None:
        types_query = types_query.filter(LeaveType.id.in_(list(leave_type_ids)))
    all_types = types_query.order_by(LeaveType.sort_order, LeaveType.id).all()
    leave_types = [t for t in all_types if not is_tiered(t)]
    tiered_ids = [t.id for t in all_types if is_tiered(t)]
    employees = _eligible_employees(db, year, employee_ids)
    months = _months_in_year(db, year)

    stats = {"year": year, "employees": len(employees), "credit_entries": 0, "carry_forward_entries": 0,
             "days_credited": ZERO, "days_carried_forward": ZERO}

    def add(employee, leave_type, entry_type, days, ref_type, ref_id, note):
        db.add(LeaveLedger(
            employee_id=employee.id, leave_type_id=leave_type.id, leave_year=year, entry_type=entry_type,
            days=days, reference_type=ref_type, reference_id=ref_id, note=note, created_by=created_by,
        ))

    for employee in employees:
        joined = employee.date_of_joining
        for leave_type in leave_types:
            if leave_type.carry_forward and not _has_entry(
                db, employee.id, leave_type.id, ENTRY_CARRY_FORWARD, REF_CARRY_FORWARD, str(year)
            ):
                carried = max(balance(db, employee.id, leave_type.id, year - 1), ZERO)
                if leave_type.carry_forward_limit is not None:
                    carried = min(carried, to_decimal(leave_type.carry_forward_limit))
                if carried > 0:
                    add(employee, leave_type, ENTRY_CARRY_FORWARD, carried, REF_CARRY_FORWARD, str(year),
                        f"Carried forward from {year - 1}")
                    stats["carry_forward_entries"] += 1
                    stats["days_carried_forward"] += carried

            entitlement = to_decimal(leave_type.annual_entitlement)
            if entitlement <= 0:
                continue

            if leave_type.credit_mode == CREDIT_MONTHLY_ACCRUAL:
                for index, (first, last) in enumerate(months, start=1):
                    if first > as_of or (joined and joined > last):
                        continue
                    ref_id = first.strftime("%Y-%m")
                    if _has_entry(db, employee.id, leave_type.id, ENTRY_CREDIT, REF_ACCRUAL, ref_id):
                        continue
                    amount = (entitlement * index / 12).quantize(Decimal("0.01")) - (
                        entitlement * (index - 1) / 12
                    ).quantize(Decimal("0.01"))
                    add(employee, leave_type, ENTRY_CREDIT, amount, REF_ACCRUAL, ref_id,
                        f"Monthly accrual {ref_id}")
                    stats["credit_entries"] += 1
                    stats["days_credited"] += amount
                continue

            if _has_entry(db, employee.id, leave_type.id, ENTRY_CREDIT, REF_YEARLY_CREDIT, str(year)):
                continue
            amount = entitlement
            note = f"Annual credit {year}"
            if leave_type.pro_rata_for_joiners and joined and joined > start:
                remaining = 12 - ((joined.year - start.year) * 12 + joined.month - start.month)
                amount = round_half_day(entitlement * remaining / 12)
                note = f"Annual credit {year} (pro-rata, {remaining} of 12 months)"
            if amount <= 0:
                continue
            add(employee, leave_type, ENTRY_CREDIT, amount, REF_YEARLY_CREDIT, str(year), note)
            stats["credit_entries"] += 1
            stats["days_credited"] += amount
    db.flush()
    if tiered_ids and start <= as_of:
        monthly = credit_catch_up(db, year, as_of=as_of, leave_type_ids=tiered_ids, employee_ids=employee_ids,
                                  created_by=created_by)
        for key in ("credit_entries", "days_credited", "carry_forward_entries", "days_carried_forward"):
            stats[key] += monthly[key]
        stats["lapse_entries"] = monthly["lapse_entries"]
        stats["days_lapsed"] = monthly["days_lapsed"]
    return stats


def adjust(
    db: Session, employee_id: int, leave_type_id: int, year: int, days: Decimal, note: str,
    created_by: Optional[int] = None, entry_type: str = ENTRY_ADJUSTMENT,
) -> LeaveLedger:
    """A manual ledger entry: an ADJUSTMENT, or the opening CARRY_FORWARD
    HR enters for the year (reference opening_carry_forward YYYY; the
    year-end carry forward uses its own carry_forward reference)."""
    if entry_type not in (ENTRY_ADJUSTMENT, ENTRY_CARRY_FORWARD):
        raise ValueError(f"Entry type {entry_type} cannot be entered manually.")
    opening = entry_type == ENTRY_CARRY_FORWARD
    row = LeaveLedger(
        employee_id=employee_id, leave_type_id=leave_type_id, leave_year=year, entry_type=entry_type,
        days=to_decimal(days), note=note, created_by=created_by,
        reference_type=REF_OPENING_CARRY_FORWARD if opening else None, reference_id=str(year) if opening else None,
    )
    db.add(row)
    db.flush()
    return row


def _application_net(db: Session, application: LeaveApplication) -> tuple[Decimal, int]:
    rows = db.query(LeaveLedger.days, LeaveLedger.leave_year).filter(
        LeaveLedger.reference_type == REF_APPLICATION,
        LeaveLedger.reference_id == str(application.id),
        LeaveLedger.entry_type.in_((ENTRY_LEAVE_DEBIT, ENTRY_REVERSAL)),
    ).all()
    net = sum((to_decimal(d) for d, _ in rows), ZERO)
    year = rows[0][1] if rows else leave_year_for(db, application.from_date)
    return net, year


def debit_for_application(
    db: Session, application: LeaveApplication, days: Optional[Decimal] = None, created_by: Optional[int] = None
) -> Optional[LeaveLedger]:
    """Writes the LEAVE_DEBIT for an approved application of a paid type
    (no-op for unpaid types or when an un-reversed debit already exists).
    `days` defaults to application.days."""
    leave_type = application.leave_type or db.get(LeaveType, application.leave_type_id)
    if not leave_type.tracks_balance:
        return None
    net, _ = _application_net(db, application)
    if net < 0:
        return None
    amount = to_decimal(application.days if days is None else days)
    if amount <= 0:
        return None
    row = LeaveLedger(
        employee_id=application.employee_id, leave_type_id=application.leave_type_id,
        leave_year=leave_year_for(db, application.from_date), entry_type=ENTRY_LEAVE_DEBIT, days=-amount,
        reference_type=REF_APPLICATION, reference_id=str(application.id),
        note=f"Leave {application.from_date.isoformat()} to {application.to_date.isoformat()}",
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    _resync_application_month(db, application, leave_type, row.leave_year, created_by)
    return row


def _resync_application_month(
    db: Session, application: LeaveApplication, leave_type: Optional[LeaveType], year: int,
    created_by: Optional[int],
) -> None:
    """Leave dated in an already lapsed month uses that month's credit, so
    its lapse is reduced (or raised again when the leave is cancelled)."""
    leave_type = leave_type or db.get(LeaveType, application.leave_type_id)
    if is_tiered(leave_type):
        tiers.resync_lapses(db, leave_type, [year], [application.employee_id], created_by)


def reverse_for_application(
    db: Session, application: LeaveApplication, created_by: Optional[int] = None, note: Optional[str] = None
) -> Optional[LeaveLedger]:
    net, year = _application_net(db, application)
    if net >= 0:
        return None
    row = LeaveLedger(
        employee_id=application.employee_id, leave_type_id=application.leave_type_id, leave_year=year,
        entry_type=ENTRY_REVERSAL, days=-net, reference_type=REF_APPLICATION, reference_id=str(application.id),
        note=note or "Leave cancelled", created_by=created_by,
    )
    db.add(row)
    db.flush()
    _resync_application_month(db, application, application.leave_type, year, created_by)
    return row


def allocate_from_balances(
    db: Session, employee_id: int, year: int, days: Decimal, codes: Iterable[str]
) -> tuple[list[tuple[LeaveType, Decimal]], Decimal]:
    """Splits `days` across the leave types in `codes` order using what each
    has available (balance minus pending); the remainder is LOP. Writes
    nothing. Intended for late-mark deductions (LATE_DEBIT rows)."""
    remaining = to_decimal(days)
    allocations = []
    for code in codes:
        if remaining <= 0:
            break
        leave_type = db.query(LeaveType).filter(LeaveType.code == code, LeaveType.is_paid.is_(True)).first()
        if leave_type is None:
            continue
        available = balance(db, employee_id, leave_type.id, year) - pending_days(
            db, employee_id, leave_type.id, year
        )
        take = min(max(available, ZERO), remaining)
        if take > 0:
            allocations.append((leave_type, take))
            remaining -= take
    return allocations, remaining


# --- monthly credit by length of service (MONTHLY_TIERED) -----------------

def _tiered_types(db: Session, leave_type_ids: Optional[Iterable[int]]) -> list[LeaveType]:
    query = db.query(LeaveType).filter(
        LeaveType.is_active.is_(True), LeaveType.is_paid.is_(True), LeaveType.credit_mode == CREDIT_MONTHLY_TIERED
    )
    if leave_type_ids is not None:
        query = query.filter(LeaveType.id.in_(list(leave_type_ids)))
    return query.order_by(LeaveType.sort_order, LeaveType.id).all()


def _monthly_stats(**extra) -> dict:
    return {**extra, "employees": 0, "credit_entries": 0, "days_credited": ZERO, "lapse_entries": 0,
            "days_lapsed": ZERO, "carry_forward_entries": 0, "days_carried_forward": ZERO,
            "missing_joining_date": []}


def _lapse_previous_month(db, leave_type, employee, usage, prev: date, stats, created_by) -> None:
    month = usage.get(tiers.month_ref(prev))
    if month is None or not month.has_credit or month.decided:
        return
    tier = tiers.employee_tier(leave_type, employee.date_of_joining, prev)
    if tier is None or tier.carry_forward:
        return
    days = month.unused
    label = f"{prev:%b %Y}"
    note = (f"Unused {leave_type.code} of {label} lapsed ({tiers.tier_label(tier)})" if days > 0
            else f"{leave_type.code} of {label} fully used, nothing to lapse ({tiers.tier_label(tier)})")
    tiers.write_lapse(db, employee.id, leave_type.id, tiers.month_ref(prev), month.leave_year, days, note, created_by)
    stats["lapse_entries"] += 1
    stats["days_lapsed"] += days


def _previous_year_state(db, leave_type, leave_year: int, ids: list[int]) -> tuple[set, dict]:
    """(employees already carried forward into leave_year, balance of leave_year - 1 by employee)."""
    done = {e for (e,) in db.query(LeaveLedger.employee_id).filter(
        LeaveLedger.leave_type_id == leave_type.id, LeaveLedger.entry_type == ENTRY_CARRY_FORWARD,
        LeaveLedger.reference_type == REF_CARRY_FORWARD, LeaveLedger.reference_id == str(leave_year),
        LeaveLedger.employee_id.in_(ids),
    )}
    sums = db.query(LeaveLedger.employee_id, func.sum(LeaveLedger.days)).filter(
        LeaveLedger.leave_type_id == leave_type.id, LeaveLedger.leave_year == leave_year - 1,
        LeaveLedger.employee_id.in_(ids),
    ).group_by(LeaveLedger.employee_id)
    previous = {e: to_decimal(total) for e, total in sums}
    for employee_id, delta in tiers.pending_corrections(db, leave_type.id, leave_year - 1, ids).items():
        previous[employee_id] = previous.get(employee_id, ZERO) - delta
    return done, previous


def _carry_forward_tiered(db, leave_type, employee, leave_year: int, year_start: date, state, stats,
                          created_by) -> None:
    done, previous = state
    joined = employee.date_of_joining
    if joined >= year_start or employee.id in done:
        return
    tier = tiers.employee_tier(leave_type, joined, tiers.add_months(year_start, -1))
    if tier is None or not tier.carry_forward:
        return
    carried = max(previous.get(employee.id, ZERO), ZERO)
    note = f"Carried forward from {leave_year - 1}"
    if tier.carry_forward_limit is not None and carried > tier.carry_forward_limit:
        carried = tier.carry_forward_limit
        note += f" (limit {tiers.fmt_days(carried)})"
    if carried <= 0:
        return
    db.add(LeaveLedger(
        employee_id=employee.id, leave_type_id=leave_type.id, leave_year=leave_year, entry_type=ENTRY_CARRY_FORWARD,
        days=carried, reference_type=REF_CARRY_FORWARD, reference_id=str(leave_year), note=note, created_by=created_by,
    ))
    stats["carry_forward_entries"] += 1
    stats["days_carried_forward"] += carried


def _credit_one_month(db, leave_type, employee, usage, first: date, last: date, leave_year, stats, created_by):
    joined = employee.date_of_joining
    if joined > last or (joined >= first and joined.day > (leave_type.joining_cutoff_day or 15)):
        return
    ref = tiers.month_ref(first)
    if ref in usage and usage[ref].has_credit:
        return
    tier = tiers.employee_tier(leave_type, joined, first)
    if tier is None or tier.days_per_month <= 0:
        return
    note = f"{leave_type.code} for {first:%b %Y} ({tiers.tier_label(tier)})"
    if joined >= first:
        note += f", joined {joined:%d %b %Y}"
    db.add(LeaveLedger(
        employee_id=employee.id, leave_type_id=leave_type.id, leave_year=leave_year, entry_type=ENTRY_CREDIT,
        days=tier.days_per_month, reference_type=tiers.REF_MONTHLY_CREDIT, reference_id=ref, note=note,
        created_by=created_by,
    ))
    stats["credit_entries"] += 1
    stats["days_credited"] += tier.days_per_month


def credit_month(
    db: Session,
    year: int,
    month: int,
    as_of: Optional[date] = None,
    leave_type_ids: Optional[Iterable[int]] = None,
    employee_ids: Optional[Iterable[int]] = None,
    created_by: Optional[int] = None,
) -> dict:
    """Monthly credit of the MONTHLY_TIERED types for every active employee.
    Per employee and type, in order:
    1. the previous month lapses when its tier has no carry forward: a LAPSE
       row removes what is left of that month's credit after the leave dated
       in that month (a 0-day row when nothing is left marks it as lapsed);
    2. lapsed months of these leave years are re-synced with later changes;
    3. at the first run of a leave year the previous year's balance is
       carried forward when the tier of its last month carries forward
       (capped at that tier's carry_forward_limit);
    4. the month is credited with days_per_month of the tier for the
       completed months of service at the month start. A joiner gets the
       joining month only when joined on/before joining_cutoff_day.
    Idempotent: (monthly_credit, YYYY-MM) and (monthly_lapse, YYYY-MM)."""
    first = date(year, month, 1)
    as_of = as_of or date.today()
    if first > as_of:
        raise ValueError(f"{first:%b %Y} has not started yet.")
    last = tiers.add_months(first, 1) - timedelta(days=1)
    prev = tiers.add_months(first, -1)
    policy = default_policy(db)
    leave_year = leave_year_of(policy, first)
    years = sorted({leave_year_of(policy, prev), leave_year})
    year_start, _ = leave_year_bounds(policy, leave_year)

    query = db.query(Employee).filter(Employee.status == "Active")
    if employee_ids is not None:
        query = query.filter(Employee.id.in_(list(employee_ids)))
    employees, missing = [], []
    for employee in query.order_by(Employee.employee_code):
        if employee.date_of_leaving and employee.date_of_leaving < first:
            continue
        if employee.date_of_joining is None:
            missing.append(employee.employee_code)
            continue
        employees.append(employee)
    ids = [e.id for e in employees]
    stats = _monthly_stats(year=year, month=month, ref=tiers.month_ref(first))
    stats["employees"] = len(employees)
    stats["missing_joining_date"] = missing

    for leave_type in _tiered_types(db, leave_type_ids):
        usage = tiers.month_usage(db, leave_type.id, years, ids)
        for employee in employees:
            _lapse_previous_month(db, leave_type, employee, usage.get(employee.id, {}), prev, stats, created_by)
        db.flush()
        entries, days = tiers.resync_lapses(db, leave_type, years, ids, created_by)
        stats["lapse_entries"] += entries
        stats["days_lapsed"] += days
        state = _previous_year_state(db, leave_type, leave_year, ids)
        for employee in employees:
            _carry_forward_tiered(db, leave_type, employee, leave_year, year_start, state, stats, created_by)
            _credit_one_month(db, leave_type, employee, usage.get(employee.id, {}), first, last, leave_year, stats,
                              created_by)
        db.flush()
    return stats


def credit_catch_up(
    db: Session,
    year: Optional[int] = None,
    as_of: Optional[date] = None,
    leave_type_ids: Optional[Iterable[int]] = None,
    employee_ids: Optional[Iterable[int]] = None,
    created_by: Optional[int] = None,
) -> dict:
    """credit_month for every month of the leave year (default: the current
    one) from its first month up to the month of `as_of` (default today), in
    order. Safe to re-run: only what is missing is written."""
    as_of = as_of or date.today()
    year = year or leave_year_for(db, as_of)
    start, end = year_bounds(db, year)
    if start > as_of:
        raise ValueError(f"Leave year {year} has not started yet.")
    stats = _monthly_stats(year=year, months=[])
    missing = set()
    month = start
    while month <= min(end, as_of):
        result = credit_month(db, month.year, month.month, as_of=as_of, leave_type_ids=leave_type_ids,
                              employee_ids=employee_ids, created_by=created_by)
        stats["months"].append(result["ref"])
        stats["employees"] = max(stats["employees"], result["employees"])
        for key in ("credit_entries", "days_credited", "lapse_entries", "days_lapsed", "carry_forward_entries",
                    "days_carried_forward"):
            stats[key] += result[key]
        missing.update(result["missing_joining_date"])
        month = tiers.add_months(month, 1)
    stats["missing_joining_date"] = sorted(missing)
    return stats


def tier_labels(leave_types: Iterable[LeaveType], employees: Iterable[Employee], on: Optional[date] = None) -> dict:
    """{(employee_id, leave_type_id): tier label} for MONTHLY_TIERED types,
    by service at the start of the month of `on` (default today)."""
    first = (on or date.today()).replace(day=1)
    employees = list(employees)
    labels = {}
    for leave_type in leave_types:
        if not is_tiered(leave_type):
            continue
        for employee in employees:
            if employee.date_of_joining is None:
                labels[(employee.id, leave_type.id)] = "Joining date missing"
                continue
            tier = tiers.employee_tier(leave_type, employee.date_of_joining, first)
            labels[(employee.id, leave_type.id)] = tiers.tier_label(tier) if tier else "No tier yet"
    return labels


def available_on(
    db: Session, employee_id: int, leave_type_id: int, day: date, include_pending: bool = True,
    exclude_id: Optional[int] = None,
) -> Decimal:
    """Days usable for leave starting on `day`: a date-aware alternative to
    balance() - pending_days(). For a MONTHLY_TIERED type in a month whose
    tier has no carry forward it is what is left of that month's own credit
    (a month not credited yet counts its tier's credit); otherwise, and as a
    cap, the year's balance."""
    year = leave_year_for(db, day)
    total = balance(db, employee_id, leave_type_id, year)
    if include_pending:
        total -= pending_days(db, employee_id, leave_type_id, year, exclude_id=exclude_id)
    leave_type, employee = db.get(LeaveType, leave_type_id), db.get(Employee, employee_id)
    if not is_tiered(leave_type) or employee is None or employee.date_of_joining is None:
        return total
    first = day.replace(day=1)
    last = tiers.add_months(first, 1) - timedelta(days=1)
    tier = tiers.employee_tier(leave_type, employee.date_of_joining, first)
    if tier is None or tier.carry_forward:
        return total
    month = tiers.month_usage(db, leave_type_id, [year], [employee_id]).get(employee_id, {}).get(tiers.month_ref(first))
    credited = month is not None and month.has_credit
    joined = employee.date_of_joining
    eligible = joined <= last and not (joined >= first and joined.day > (leave_type.joining_cutoff_day or 15))
    left = (month.credit if credited else (tier.days_per_month if eligible else ZERO)) - (month.used if month else ZERO)
    if include_pending:
        query = db.query(func.coalesce(func.sum(LeaveApplication.days), 0)).filter(
            LeaveApplication.employee_id == employee_id, LeaveApplication.leave_type_id == leave_type_id,
            LeaveApplication.status.in_(PENDING_STATUSES), LeaveApplication.from_date >= first,
            LeaveApplication.from_date <= last,
        )
        if exclude_id is not None:
            query = query.filter(LeaveApplication.id != exclude_id)
        left -= to_decimal(query.scalar())
    return min(total, left) if credited else left
