"""Dashboard payroll figures. A run belongs to the month of its period_end.
Totals come from Included line items of runs that have figures
(Calculated / PendingApproval / Approved / Paid)."""
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.pay_group import PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun, PayrollRunLineItem
from app.models.salary_component import SalaryComponent

COUNTED_STATUSES = ("Calculated", "PendingApproval", "Approved", "Paid")
EXCLUDED_STATUSES = ("Cancelled",)
TREND_MONTHS = 6
TDS_CODE = "TDS"


def month_key(day: date) -> str:
    return day.strftime("%Y-%m")


def month_label(key: str) -> str:
    return date(int(key[:4]), int(key[5:]), 1).strftime("%b %Y")


def _shift(key: str, months_back: int) -> str:
    index = int(key[:4]) * 12 + int(key[5:]) - 1 - months_back
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _money(value) -> float:
    return float(round(Decimal(str(value or 0)), 2))


def _line_items(db: Session, run_ids: list[int]) -> list[PayrollRunLineItem]:
    if not run_ids:
        return []
    stmt = select(PayrollRunLineItem).where(
        PayrollRunLineItem.payroll_run_id.in_(run_ids), PayrollRunLineItem.status == "Included"
    )
    return list(db.scalars(stmt))


def _tds_codes(db: Session) -> set[str]:
    codes = db.scalars(select(SalaryComponent.code).where(SalaryComponent.statutory_type == "TDS"))
    return {TDS_CODE, *codes}


def _tds(items: list[PayrollRunLineItem], codes: set[str]) -> Decimal:
    return sum(
        (
            Decimal(str(entry.get("resolved_amount") or 0))
            for item in items
            for entry in (item.computation_breakdown or [])
            if entry.get("component_type") == "Deduction" and entry.get("component_code") in codes
        ),
        Decimal("0"),
    )


def _totals(items: list[PayrollRunLineItem]) -> dict:
    return {
        "gross": _money(sum(Decimal(str(i.gross_earnings or 0)) for i in items)),
        "deductions": _money(sum(Decimal(str(i.total_deductions or 0)) for i in items)),
        "net": _money(sum(Decimal(str(i.net_pay or 0)) for i in items)),
        "employees": len({i.employee_id for i in items}),
    }


def payroll_overview(db: Session, as_of: date) -> tuple[Optional[dict], list[dict], list[dict]]:
    """(latest month summary or None, 6-month trend, last 6 runs)."""
    runs = list(db.scalars(select(PayrollRun).where(PayrollRun.status.notin_(EXCLUDED_STATUSES))))
    by_month: dict[str, list[PayrollRun]] = defaultdict(list)
    for run in runs:
        by_month[month_key(run.period_end)].append(run)

    counted_months = [m for m, rs in by_month.items() if any(r.status in COUNTED_STATUSES for r in rs)]
    latest = max(counted_months or by_month or [None], key=lambda m: m or "")

    summary = None
    if latest:
        month_runs = by_month[latest]
        items = _line_items(db, [r.id for r in month_runs if r.status in COUNTED_STATUSES])
        totals = _totals(items)
        summary = {
            "month": latest, "label": month_label(latest),
            "gross": totals["gross"], "deductions": totals["deductions"], "net": totals["net"],
            "tds": _money(_tds(items, _tds_codes(db))), "employees_paid": totals["employees"],
            "runs": len(month_runs), "status_counts": dict(Counter(r.status for r in month_runs)),
        }

    end = latest or month_key(as_of)
    months = [_shift(end, back) for back in range(TREND_MONTHS - 1, -1, -1)]
    month_of_run = {
        r.id: key for key in months for r in by_month.get(key, []) if r.status in COUNTED_STATUSES
    }
    items_by_month: dict[str, list[PayrollRunLineItem]] = defaultdict(list)
    for item in _line_items(db, list(month_of_run)):
        items_by_month[month_of_run[item.payroll_run_id]].append(item)
    trend = [
        {"month": key, "label": month_label(key), **_totals(items_by_month.get(key, []))} for key in months
    ]

    return summary, trend, recent_runs(db)


def recent_runs(db: Session, limit: int = 6) -> list[dict]:
    stmt = (
        select(PayrollRun, PayGroup.name, PaySchedule.schedule_name)
        .outerjoin(PayGroup, PayGroup.id == PayrollRun.pay_group_id)
        .outerjoin(PaySchedule, PaySchedule.id == PayrollRun.pay_schedule_id)
        .order_by(PayrollRun.id.desc())
        .limit(limit)
    )
    return [
        {
            "id": run.id, "pay_group": group_name or schedule_name or "-",
            "period_start": run.period_start, "period_end": run.period_end,
            "period_label": month_label(month_key(run.period_end)), "pay_date": run.pay_date,
            "status": run.status, "source": run.source,
            "employees": run.employee_count or 0, "net": _money(run.total_net),
        }
        for run, group_name, schedule_name in db.execute(stmt)
    ]
