"""Payroll workbook import: preview (extract + match, nothing written) and
commit (pay profiles, variable inputs and one Draft run per pay group, in
one transaction)."""
from __future__ import annotations

import calendar
import re
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.pay_schedule import PaySchedule
from app.models.payroll_run import PayrollRun
from app.models.variable_pay import VariablePayInput
from app.services.payroll_engine.pay_groups import PayGroupCalculationError, PayGroupParamsError, get_strategy
from app.services.payroll_engine.pay_groups.base import dec
from app.services.payroll_import.excel_extract import (
    PROFILE_FIELDS,
    extract_workbook,
    inputs_to_dict,
    profile_from_dict,
    profile_to_dict,
)
from app.services.payroll_import.matching import (
    AMBIGUOUS,
    MATCH_STATUSES,
    MATCHED_BY_ID,
    MATCHED_BY_NAME,
    EmployeeMatcher,
    MatchResult,
    employee_display_name,
    mark_duplicates,
)

IMPORT_SOURCE = "ExcelImport"
SCHEDULE_NAME = "Monthly (Pay Groups)"
RECALCULABLE_RUN_STATUSES = ("Draft", "Calculated", "Failed")


class PayrollImportError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def parse_period(value: str) -> tuple[date, date]:
    match = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", value or "")
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise PayrollImportError("period must be YYYY-MM, e.g. 2026-09")
    year, month = int(match.group(1)), int(match.group(2))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def _float(value) -> Optional[float]:
    return float(value) if value is not None else None


# --- preview -----------------------------------------------------------------------

def preview(db: Session, file_bytes: bytes, period: str) -> dict[str, Any]:
    period_start, period_end = parse_period(period)
    groups = {g.code: g for g in db.query(PayGroup).all()}
    try:
        sheets, missing = extract_workbook(file_bytes, {code: g.params or {} for code, g in groups.items()})
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types for bad files
        raise PayrollImportError(f"Could not read the workbook: {exc}") from exc
    if not sheets:
        raise PayrollImportError("None of the expected payroll sheets were found in the workbook.")

    matcher = EmployeeMatcher.from_db(db)
    matched: list[tuple[str, int, MatchResult]] = []
    for sheet in sheets:
        for row in sheet.rows:
            matched.append((sheet.sheet_name, row.row_number, matcher.match(row.excel_id, row.excel_name)))
    mark_duplicates(matched)
    result_by_row = {(s, r): m for s, r, m in matched}

    response_sheets, batch_rows = [], []
    totals: Counter = Counter()
    per_sheet: dict[str, dict] = {}
    for sheet in sheets:
        group = groups.get(sheet.spec.pay_group_code)
        counts: Counter = Counter()
        rows_out = []
        for row in sheet.rows:
            m = result_by_row[(sheet.sheet_name, row.row_number)]
            notes = list(m.notes)
            if row.error:
                notes.append(row.error)
            if row.case is not None:
                notes.extend(row.case.notes)
            profile = profile_to_dict(row.case.profile) if row.case else None
            inputs = inputs_to_dict(row.case.inputs) if row.case else None
            computed = None
            if row.case is not None and group is not None:
                employee = matcher.by_id.get(m.employee_id) if m.employee_id else None
                try:
                    computed = float(get_strategy(group.calc_model).calculate(
                        row.case.profile, employee, row.case.inputs, group.params or {}).net_pay)
                except (PayGroupCalculationError, PayGroupParamsError) as exc:
                    notes.append(f"cannot calculate: {exc}")
            if row.case is not None and (computed is None or abs(dec(computed) - row.case.excel_net) > 1):
                # Explain a difference by the hand-edited cells that feed Excel's net pay.
                notes.extend(f"Excel {col} ({header}) {desc}; the system calculates it from the rates"
                             for col, header, desc in row.overrides)
            counts[m.match_status] += 1
            rows_out.append({
                "row_number": row.row_number,
                "excel_name": row.excel_name,
                "excel_id": row.excel_id,
                "match_status": m.match_status,
                "employee_id": m.employee_id,
                "employee_code": m.employee_code,
                "employee_name": m.employee_name,
                "score": m.score,
                "candidates": [c.as_dict() for c in m.candidates],
                "profile": profile,
                "inputs": inputs,
                "excel_net_pay": _float(row.case.excel_net) if row.case else None,
                "computed_net_pay": computed,
                "notes": notes,
            })
            batch_rows.append({
                "sheet": sheet.sheet_name, "row_number": row.row_number, "pay_group_code": sheet.spec.pay_group_code,
                "match_status": m.match_status, "employee_id": m.employee_id, "excel_name": row.excel_name,
                "profile": profile, "inputs": inputs, "error": row.error,
            })
        totals.update(counts)
        per_sheet[sheet.sheet_name] = {s: counts.get(s, 0) for s in MATCH_STATUSES} | {"rows": len(sheet.rows)}
        response_sheets.append({
            "sheet": sheet.sheet_name,
            "pay_group_code": sheet.spec.pay_group_code,
            "pay_group_id": group.id if group else None,
            "pay_group_name": group.name if group else None,
            "rows": rows_out,
        })

    warnings = [f"Sheet '{name}' was not found in the workbook." for name in missing]
    warnings += [f"Pay group {s['pay_group_code']} does not exist - sheet '{s['sheet']}' cannot be committed."
                 for s in response_sheets if s["pay_group_id"] is None]
    summary = {
        "sheets": per_sheet,
        "total": {s: totals.get(s, 0) for s in MATCH_STATUSES} | {"rows": sum(totals.values())},
    }
    return {
        "period": f"{period_start:%Y-%m}",
        "period_start": period_start,
        "period_end": period_end,
        "sheets": response_sheets,
        "summary": summary,
        "warnings": warnings,
        "_batch_rows": batch_rows,
    }


# --- commit ------------------------------------------------------------------------

def _same_number(a, b) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(dec(a) - dec(b)) < Decimal("0.0001")


def profile_differs(current: Optional[EmployeePayProfile], group_id: int, data: dict) -> bool:
    if current is None or current.pay_group_id != group_id:
        return True
    for name in PROFILE_FIELDS:
        new, old = data.get(name), getattr(current, name)
        if name in ("pf_applicable", "esi_applicable"):
            if new != old:
                return True
        elif name == "skill_level":
            if (new or None) != (old or None):
                return True
        elif not _same_number(new, old):
            return True
    return (data.get("extra") or {}) != (current.extra or {})


def _monthly_schedule(db: Session) -> PaySchedule:
    schedule = (
        db.query(PaySchedule)
        .filter(PaySchedule.is_active.is_(True), PaySchedule.pay_frequency == "Monthly")
        .order_by(PaySchedule.id)
        .first()
    )
    if schedule is None:
        schedule = PaySchedule(
            schedule_name=SCHEDULE_NAME, pay_frequency="Monthly", payment_day_rule={"type": "LastDayOfMonth"},
            holiday_adjustment="Before", processing_day_offset=0, is_active=True,
            description="Created by the payroll Excel import for pay-group runs.",
        )
        db.add(schedule)
        db.flush()
    return schedule


def _ref(row: dict) -> dict:
    return {"sheet": row["sheet"], "row_number": row["row_number"], "excel_name": row.get("excel_name")}


def _resolve_rows(db: Session, rows: list[dict], decisions: list[dict]):
    by_key = {(r["sheet"], r["row_number"]): r for r in rows}
    decided: dict[tuple, dict] = {}
    for d in decisions:
        key = (d.get("sheet"), d.get("row_number"))
        if key not in by_key:
            raise PayrollImportError(f"Decision for unknown row: sheet {key[0]!r} row {key[1]}.")
        decided[key] = d

    resolved, skipped = [], []
    for row in rows:
        decision = decided.get((row["sheet"], row["row_number"]))
        if decision and decision.get("skip"):
            skipped.append({**_ref(row), "reason": "skipped by reviewer"})
            continue
        employee_id = decision.get("employee_id") if decision else None
        if employee_id is None:
            if row["match_status"] not in (MATCHED_BY_ID, MATCHED_BY_NAME):
                status = "ambiguous" if row["match_status"] == AMBIGUOUS else "unmatched"
                skipped.append({**_ref(row), "reason": f"needs review ({status})"})
                continue
            employee_id = row["employee_id"]
        if row.get("profile") is None:
            skipped.append({**_ref(row), "reason": row.get("error") or "row could not be read"})
            continue
        employee = db.get(Employee, employee_id)
        if employee is None:
            raise PayrollImportError(
                f"Employee {employee_id} (sheet {row['sheet']!r} row {row['row_number']}) does not exist.")
        resolved.append((row, employee))

    seen: dict[int, dict] = {}
    clashes = []
    for row, employee in resolved:
        first = seen.setdefault(employee.id, row)
        if first is not row:
            clashes.append(f"{employee.employee_code}: {first['sheet']!r} row {first['row_number']} and "
                           f"{row['sheet']!r} row {row['row_number']}")
    if clashes:
        raise PayrollImportError("An employee can be imported from one row only: " + "; ".join(clashes))
    return resolved, skipped


def _plan(db: Session, resolved, groups: dict, period_start: date, skipped: list) -> list:
    """Validate every row before anything is written."""
    plan = []
    for row, employee in resolved:
        group = groups.get(row["pay_group_code"])
        if group is None or not group.is_active:
            skipped.append({**_ref(row), "reason": f"pay group {row['pay_group_code']} is missing or inactive"})
            continue
        errors = get_strategy(group.calc_model).validate_profile(profile_from_dict(row["profile"]), group.params or {})
        if errors:
            skipped.append({**_ref(row), "reason": "invalid pay profile: " + "; ".join(errors)})
            continue
        open_profiles = (
            db.query(EmployeePayProfile)
            .filter(EmployeePayProfile.employee_id == employee.id, EmployeePayProfile.effective_to.is_(None))
            .order_by(EmployeePayProfile.effective_from.desc(), EmployeePayProfile.id.desc())
            .all()
        )
        if any(p.effective_from > period_start for p in open_profiles):
            skipped.append({**_ref(row), "reason": "employee has a pay profile starting after this period"})
            continue
        plan.append((row, employee, group, open_profiles))
    return plan


def _existing_runs(db: Session, groups: dict, period_start: date, period_end: date) -> dict[str, PayrollRun]:
    runs = {}
    for code, group in groups.items():
        existing = (
            db.query(PayrollRun)
            .filter(PayrollRun.pay_group_id == group.id, PayrollRun.period_start == period_start,
                    PayrollRun.period_end == period_end, PayrollRun.status != "Cancelled")
            .order_by(PayrollRun.id.desc())
            .all()
        )
        frozen = [r for r in existing if r.status not in RECALCULABLE_RUN_STATUSES]
        if frozen:
            raise PayrollImportError(
                f"Payroll run #{frozen[0].id} for {code} {period_start:%Y-%m} is already {frozen[0].status}; "
                "cancel it before re-importing.", status_code=409)
        if existing:
            runs[code] = existing[0]
    return runs


def _upsert_inputs(db: Session, batch_id: str, row: dict, employee: Employee, period_start: date,
                   period_end: date) -> bool:
    inputs = row["inputs"]
    vi = (
        db.query(VariablePayInput)
        .filter(VariablePayInput.employee_id == employee.id, VariablePayInput.period_start == period_start,
                VariablePayInput.period_end == period_end)
        .first()
    )
    created = vi is None
    if created:
        vi = VariablePayInput(employee_id=employee.id, period_start=period_start, period_end=period_end)
        db.add(vi)
    vi.source = IMPORT_SOURCE
    vi.status = "Draft"
    vi.import_batch_id = batch_id
    vi.present_days = inputs.get("present_days")
    vi.lop_days = inputs.get("lop_days") or 0
    vi.paid_leave_days = 0
    vi.overtime_hours = inputs.get("overtime_hours") or 0
    vi.overtime_days = inputs.get("overtime_days") or 0
    vi.overtime_amount = None
    vi.ad_hoc_earnings = [{**i, "remarks": "Excel import"} for i in inputs.get("ad_hoc_earnings") or []]
    vi.ad_hoc_deductions = [{**i, "remarks": "Excel import"} for i in inputs.get("ad_hoc_deductions") or []]
    vi.remarks = f"Imported from {row['sheet']!r} row {row['row_number']}"
    return created


def commit(db: Session, batch_id: str, meta: dict, rows: list[dict], decisions: list[dict]) -> dict[str, Any]:
    period_start, period_end = date.fromisoformat(meta["period_start"]), date.fromisoformat(meta["period_end"])
    resolved, skipped = _resolve_rows(db, rows, decisions)
    groups = {g.code: g for g in db.query(PayGroup).all()}

    employee_ids = [e.id for _, e in resolved]
    if employee_ids and db.query(VariablePayInput).filter(
        VariablePayInput.employee_id.in_(employee_ids),
        VariablePayInput.period_start == period_start,
        VariablePayInput.period_end == period_end,
        VariablePayInput.status == "Locked",
    ).first():
        raise PayrollImportError(
            f"Variable inputs for {period_start:%Y-%m} are Locked - a payroll run has already consumed them, "
            "so this import cannot replace them.", status_code=409)

    plan = _plan(db, resolved, groups, period_start, skipped)
    used_groups = {group.code: group for _, _, group, _ in plan}
    runs_by_group = _existing_runs(db, used_groups, period_start, period_end)

    counts = Counter()
    committed_rows = []
    runs_out = []
    try:
        for row, employee, group, open_profiles in plan:
            data = row["profile"]
            if profile_differs(open_profiles[0] if open_profiles else None, group.id, data):
                for previous in open_profiles:
                    previous.effective_to = period_start - timedelta(days=1)
                db.add(EmployeePayProfile(
                    employee_id=employee.id, pay_group_id=group.id, effective_from=period_start, effective_to=None,
                    **{k: data.get(k) for k in PROFILE_FIELDS}, extra=dict(data.get("extra") or {}),
                ))
                counts["profiles_created"] += 1
            else:
                counts["profiles_unchanged"] += 1
            created = _upsert_inputs(db, batch_id, row, employee, period_start, period_end)
            counts["inputs_created" if created else "inputs_updated"] += 1
            committed_rows.append({**_ref(row), "employee_id": employee.id, "employee_code": employee.employee_code,
                                   "employee_name": employee_display_name(employee), "pay_group_code": group.code})

        schedule = None
        for code, group in used_groups.items():
            run = runs_by_group.get(code)
            created = run is None
            if created:
                schedule = schedule or _monthly_schedule(db)
                run = PayrollRun(
                    pay_schedule_id=schedule.id, pay_group_id=group.id, period_start=period_start,
                    period_end=period_end, pay_date=period_end, run_type="Regular", status="Draft",
                    source="InAppCalculation", created_by="Payroll Excel import",
                )
                db.add(run)
                db.flush()
            else:
                run.status = "Draft"  # inputs changed: the run must be recalculated
            runs_out.append({"pay_group_code": code, "pay_group_id": group.id, "pay_group_name": group.name,
                             "run_id": run.id, "created": created, "status": run.status,
                             "employees": sum(1 for r in committed_rows if r["pay_group_code"] == code)})
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "period": f"{period_start:%Y-%m}",
        "committed": len(committed_rows),
        **{k: counts.get(k, 0) for k in ("profiles_created", "profiles_unchanged", "inputs_created", "inputs_updated")},
        "runs": runs_out,
        "rows": committed_rows,
        "skipped": skipped,
    }
