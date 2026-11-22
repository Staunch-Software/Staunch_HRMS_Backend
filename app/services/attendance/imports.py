"""Attendance uploads: preview (read + match, nothing written) and commit.

DAILY         Employee ID, Employee Name, Date, In Time, Out Time, Reason
              -> TIMES overrides (source UPLOAD_DAILY)
MUSTER        Employee ID, Employee Name, day columns 1..31 (or dates of the
              month) holding P/A/HD/WO/H/OD or a leave code; blank = no change
              -> STATUS overrides (source UPLOAD_MUSTER)
SITE_MONTHLY  Employee ID, Employee Name, Days Present, OT Days, OT Hours, LOP Days
              -> VariablePayInput for the month (source AttendanceUpload)

Rows are matched with EmployeeMatcher (ID first, then name); AMBIGUOUS and
UNMATCHED rows need an HR decision. Errors that depend on the matched
employee (no pay profile, Locked inputs, duplicate day) are re-checked at
commit for the employee HR chose. Re-committing the same file creates no new
override rows. Monthly processing reads the result through
app.services.attendance.effective.effective_days.
"""
from __future__ import annotations

import calendar
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any, Callable, Optional

from openpyxl import load_workbook
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.pay_group import EmployeePayProfile, PayGroup
from app.models.variable_pay import VariablePayInput
from app.services.attendance.effective import active_overrides, worked_minutes
from app.services.attendance.overrides import (
    KIND_STATUS,
    KIND_TIMES,
    SOURCE_UPLOAD_DAILY,
    SOURCE_UPLOAD_MUSTER,
    OverrideError,
    active_leave_codes,
    build_values,
    format_time,
    parse_date_value,
    parse_day_code,
    parse_time_value,
    save_override,
)
from app.services.excel_import.common import clean_str, to_number
from app.services.payroll_import.matching import (
    AMBIGUOUS,
    MATCHED_BY_ID,
    MATCHED_BY_NAME,
    UNMATCHED,
    EmployeeMatcher,
    MatchResult,
    employee_display_name,
    mark_duplicates,
)
from app.services.variable_pay_inputs import find_input, locked_employee_ids

KIND_DAILY = "DAILY"
KIND_MUSTER = "MUSTER"
KIND_SITE_MONTHLY = "SITE_MONTHLY"
KINDS = (KIND_DAILY, KIND_MUSTER, KIND_SITE_MONTHLY)

INPUT_SOURCE = "AttendanceUpload"
BIOMETRIC_GROUP_CODE = "CHENNAI_HO"
DAILY_DEFAULT_REASON = "Daily corrections upload"
MUSTER_REASON = "Monthly muster upload"
HEADER_SCAN_ROWS = 15
WEEKDAY_PREFIXES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

COLUMN_ALIASES = {
    "employee_id": {"employeeid", "empid", "employeecode", "empcode", "employeeno", "empno", "staffid",
                    "stempid", "id", "code", "idno"},
    "employee_name": {"employeename", "name", "empname", "staffname", "nameofemployee", "nameoftheemployee"},
    "date": {"date", "workdate", "attendancedate"},
    "in_time": {"intime", "in", "punchin", "timein", "checkin", "login"},
    "out_time": {"outtime", "out", "punchout", "timeout", "checkout", "logout"},
    "reason": {"reason", "remarks", "remark", "comment", "comments", "note", "notes"},
    "days_present": {"dayspresent", "presentdays", "present", "noofdayspresent", "daysworked", "workeddays"},
    "ot_days": {"otdays", "overtimedays"},
    "ot_hours": {"othours", "overtimehours", "othrs"},
    "lop_days": {"lopdays", "lop", "lossofpaydays", "lossofpay"},
}
KIND_FIELDS = {
    KIND_DAILY: ("date", "in_time", "out_time", "reason"),
    KIND_MUSTER: (),
    KIND_SITE_MONTHLY: ("days_present", "ot_days", "ot_hours", "lop_days"),
}
REQUIRED_FIELDS = {KIND_DAILY: ("date", "in_time", "out_time"), KIND_MUSTER: (), KIND_SITE_MONTHLY: ("days_present",)}
FIELD_LABELS = {
    "employee_id": "Employee ID", "employee_name": "Employee Name", "date": "Date", "in_time": "In Time",
    "out_time": "Out Time", "reason": "Reason", "days_present": "Days Present", "ot_days": "OT Days",
    "ot_hours": "OT Hours", "lop_days": "LOP Days",
}
_DAY_HEADER = re.compile(r"^(\d{1,2})(?:\s*[\-(/,.]?\s*([A-Za-z]{2,9})\.?\)?)?$")


class AttendanceImportError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def parse_kind(value: str) -> str:
    kind = re.sub(r"[\s\-]+", "_", (value or "").strip()).upper()
    if kind not in KINDS:
        raise AttendanceImportError(f"Unknown upload kind {value!r}: use daily, muster or site-monthly.", 404)
    return kind


def batch_mode(kind: str) -> str:
    return f"attendance-{kind.lower().replace('_', '-')}"


def parse_month(value: str) -> tuple[date, date]:
    match = re.fullmatch(r"\s*(\d{4})-(\d{1,2})\s*", value or "")
    if not match or not 1 <= int(match.group(2)) <= 12:
        raise AttendanceImportError("month must be YYYY-MM, e.g. 2026-11")
    year, month = int(match.group(1)), int(match.group(2))
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


# --- reading the sheet -----------------------------------------------------------------

@dataclass
class SheetRow:
    row_number: int
    excel_id: Optional[str]
    excel_name: Optional[str]
    cells: dict[str, Any]
    days: dict[int, Any] = field(default_factory=dict)


def _norm_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) if value is not None else ""


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _is_weekday_text(value: Any) -> bool:
    text = str(value).strip().lower().rstrip(".")
    return text.isalpha() and len(text) <= 9 and text[:3] in WEEKDAY_PREFIXES


def _id_text(value: Any) -> Optional[str]:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return clean_str(value)


def _day_of_header(value: Any, month_start: date) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        value = value.date()
    if isinstance(value, date):
        if (value.year, value.month) == (month_start.year, month_start.month):
            return value.day
        raise AttendanceImportError(f"Column header {value:%d-%m-%Y} is not a day of {month_start:%Y-%m}.")
    if isinstance(value, (int, float)):
        return int(value) if float(value).is_integer() and 1 <= value <= 31 else None
    text = str(value).strip()
    match = _DAY_HEADER.match(text)
    if match:
        day, weekday = int(match.group(1)), match.group(2)
        return day if 1 <= day <= 31 and (weekday is None or _is_weekday_text(weekday)) else None
    if re.search(r"\d", text) and re.search(r"[-/.]", text):
        try:
            parsed = parse_date_value(text)
        except ValueError:
            return None
        return _day_of_header(parsed, month_start)
    return None


def _map_header(row: tuple, kind: str, month_start: date) -> tuple[dict[str, int], dict[int, int]]:
    columns: dict[str, int] = {}
    days: dict[int, int] = {}
    wanted = ("employee_id", "employee_name") + KIND_FIELDS[kind]
    for index, value in enumerate(row):
        key = _norm_header(value)
        name = next((f for f in wanted if key in COLUMN_ALIASES[f]), None)
        if name is not None:
            columns.setdefault(name, index)
            continue
        if kind == KIND_MUSTER:
            day = _day_of_header(value, month_start)
            if day is not None:
                if day in days:
                    raise AttendanceImportError(f"Day {day} appears in more than one column header.")
                days[day] = index
    return columns, days


def _missing(kind: str, columns: dict[str, int], days: dict[int, int]) -> list[str]:
    missing = [FIELD_LABELS[f] for f in REQUIRED_FIELDS[kind] if f not in columns]
    if kind == KIND_MUSTER and not days:
        missing.append("day columns 1..31")
    return missing


def read_sheet(file_bytes: bytes, kind: str, month_start: date) -> tuple[str, list[SheetRow]]:
    """First worksheet with a usable header row (within the first rows):
    returns (sheet name, data rows). Fully blank rows and the weekday-name row
    under a muster header are dropped."""
    if not file_bytes:
        raise AttendanceImportError("The uploaded file is empty.")
    try:
        workbook = load_workbook(BytesIO(file_bytes), read_only=True, data_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types for a bad file
        raise AttendanceImportError(f"Could not read the uploaded file as an .xlsx workbook: {exc}") from exc
    try:
        problem = None
        for sheet in workbook.worksheets:
            values = [tuple(r) for r in sheet.iter_rows(values_only=True)]
            for header_index, header in enumerate(values[:HEADER_SCAN_ROWS]):
                columns, days = _map_header(header, kind, month_start)
                if "employee_id" not in columns and "employee_name" not in columns:
                    continue
                missing = _missing(kind, columns, days)
                if missing:
                    problem = problem or f"Sheet '{sheet.title}' is missing column(s): {', '.join(missing)}."
                    continue
                return sheet.title, _data_rows(values, header_index, columns, days)
        raise AttendanceImportError(
            problem or "No header row with 'Employee ID' or 'Employee Name' was found - use the template."
        )
    finally:
        workbook.close()


def _data_rows(values: list[tuple], header_index: int, columns: dict[str, int], days: dict[int, int]) -> list[SheetRow]:
    def cell(row: tuple, index: Optional[int]) -> Any:
        if index is None or index >= len(row):
            return None
        value = row[index]
        return value.strip() if isinstance(value, str) else value

    rows = []
    for index in range(header_index + 1, len(values)):
        raw = values[index]
        if all(_blank(v) for v in raw):
            continue
        excel_id = _id_text(cell(raw, columns.get("employee_id")))
        excel_name = clean_str(cell(raw, columns.get("employee_name")))
        if excel_id is None and excel_name is None and all(_blank(v) or _is_weekday_text(v) for v in raw):
            continue
        rows.append(SheetRow(
            row_number=index + 1,
            excel_id=excel_id,
            excel_name=excel_name,
            cells={name: cell(raw, col) for name, col in columns.items()},
            days={day: cell(raw, col) for day, col in days.items()},
        ))
    return rows


# --- per-kind row parsing ----------------------------------------------------------------

@dataclass
class ParsedRow:
    data: dict[str, Any]
    payload: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _parse_daily(row: SheetRow, month_start: date, month_end: date, _ctx: dict) -> Optional[ParsedRow]:
    raw_date, raw_in, raw_out = (row.cells.get(f) for f in ("date", "in_time", "out_time"))
    if all(_blank(v) for v in (raw_date, raw_in, raw_out)):
        return None
    errors: list[str] = []
    work_date = t_in = t_out = None
    try:
        work_date = parse_date_value(raw_date)
        if work_date is None:
            errors.append("Date is required.")
        elif not month_start <= work_date <= month_end:
            errors.append(f"Date {work_date:%d-%m-%Y} is outside {month_start:%Y-%m}.")
    except ValueError as exc:
        errors.append(f"Date: {exc}.")
    times_ok = True
    for label, raw in (("In Time", raw_in), ("Out Time", raw_out)):
        try:
            parsed = parse_time_value(raw)
        except ValueError as exc:
            errors.append(f"{label}: {exc}.")
            times_ok = False
            continue
        if label == "In Time":
            t_in = parsed
        else:
            t_out = parsed
    if times_ok and t_in is None and t_out is None:
        errors.append("In Time and Out Time are both blank.")
    if t_in is not None and t_out is not None and t_out <= t_in:
        errors.append(f"Out Time {format_time(t_out)} is not after In Time {format_time(t_in)} "
                      "(overnight shifts are not supported).")
    reason = clean_str(row.cells.get("reason"))
    minutes = None
    if work_date and t_in and t_out and t_out > t_in:
        minutes = worked_minutes(datetime.combine(work_date, t_in), datetime.combine(work_date, t_out))
    return ParsedRow(
        data={"date": work_date.isoformat() if work_date else None, "in_time": format_time(t_in),
              "out_time": format_time(t_out), "worked_minutes": minutes, "reason": reason},
        payload={"work_date": work_date.isoformat() if work_date else None,
                 "punch_in": t_in.isoformat() if t_in else None, "punch_out": t_out.isoformat() if t_out else None,
                 "reason": reason or DAILY_DEFAULT_REASON},
        errors=errors,
    )


def _code_label(status: str, leave: Optional[str]) -> str:
    if status == "LEAVE":
        return leave or "LEAVE"
    return f"{status}/{leave}" if leave else status


def _parse_muster(row: SheetRow, month_start: date, month_end: date, ctx: dict) -> Optional[ParsedRow]:
    errors: list[str] = []
    unchecked: set[str] = set()
    codes: dict[int, tuple[str, Optional[str]]] = {}
    for day in sorted(row.days):
        raw = row.days[day]
        if _blank(raw):
            continue
        if day > month_end.day:
            errors.append(f"Day {day}: {month_start:%Y-%m} has only {month_end.day} days - leave it blank.")
            continue
        try:
            status, leave, warning = parse_day_code(raw, ctx["leave_codes"])
        except OverrideError as exc:
            errors.append(f"Day {day}: {exc}.")
            continue
        if warning and leave:
            unchecked.add(leave)
        codes[day] = (status, leave)
    if not codes and not errors:
        return None
    warnings = []
    if unchecked:
        ctx["unchecked_leave_codes"].update(unchecked)
        warnings.append(f"Leave types are not set up yet; {', '.join(sorted(unchecked))} accepted without checking.")
    counts = Counter(status for status, _ in codes.values())
    return ParsedRow(
        data={"days": {str(d): _code_label(*v) for d, v in codes.items()}, "counts": dict(counts)},
        payload={"days": {str(d): [s, lv] for d, (s, lv) in codes.items()}},
        errors=errors,
        warnings=warnings,
    )


def _parse_site(row: SheetRow, month_start: date, month_end: date, _ctx: dict) -> Optional[ParsedRow]:
    names = ("days_present", "ot_days", "ot_hours", "lop_days")
    if all(_blank(row.cells.get(n)) for n in names):
        return None
    days_in_month = month_end.day
    errors: list[str] = []
    values: dict[str, Optional[float]] = {}
    for name in names:
        value, error = to_number(row.cells.get(name), required=name == "days_present", field_name=FIELD_LABELS[name])
        if error:
            errors.append(error)
        values[name] = value
    for name in ("days_present", "ot_days", "lop_days"):
        value = values[name]
        if value is not None and not 0 <= value <= days_in_month:
            errors.append(f"{FIELD_LABELS[name]} must be between 0 and {days_in_month} (got {value:g}).")
    if values["ot_hours"] is not None and values["ot_hours"] < 0:
        errors.append(f"OT Hours cannot be negative (got {values['ot_hours']:g}).")
    warnings = []
    present, lop = values["days_present"], values["lop_days"]
    if present is not None and lop is not None and present + lop > days_in_month:
        warnings.append(f"Days Present + LOP Days is {present + lop:g}, more than the {days_in_month} days in the month.")
    return ParsedRow(data={**values, "pay_group": None}, payload=dict(values), errors=errors, warnings=warnings)


PARSERS: dict[str, Callable[..., Optional[ParsedRow]]] = {
    KIND_DAILY: _parse_daily, KIND_MUSTER: _parse_muster, KIND_SITE_MONTHLY: _parse_site,
}


# --- employee-dependent checks -------------------------------------------------------------

def month_profiles(db: Session, employee_ids, month_start: date, month_end: date) -> dict[int, tuple[EmployeePayProfile, PayGroup]]:
    """Latest pay profile overlapping the month, per employee."""
    ids = list(set(employee_ids))
    if not ids:
        return {}
    rows = (
        db.query(EmployeePayProfile, PayGroup)
        .join(PayGroup, PayGroup.id == EmployeePayProfile.pay_group_id)
        .filter(
            EmployeePayProfile.employee_id.in_(ids),
            EmployeePayProfile.effective_from <= month_end,
            or_(EmployeePayProfile.effective_to.is_(None), EmployeePayProfile.effective_to >= month_start),
        )
        .order_by(EmployeePayProfile.effective_from.desc(), EmployeePayProfile.id.desc())
    )
    result: dict[int, tuple[EmployeePayProfile, PayGroup]] = {}
    for profile, group in rows:
        result.setdefault(profile.employee_id, (profile, group))
    return result


def _locked_message(month_start: date) -> str:
    return (f"Monthly inputs for {month_start:%Y-%m} are Locked - a payroll run has already used them, "
            "so they cannot be replaced.")


def _site_employee_checks(profile_group: Optional[tuple], locked: bool, month_start: date) -> tuple[list, list]:
    errors, warnings = [], []
    if profile_group is None:
        errors.append(f"No pay profile for {month_start:%Y-%m} - assign a pay group first.")
    elif profile_group[1].code == BIOMETRIC_GROUP_CODE:
        warnings.append(f"Pay group {BIOMETRIC_GROUP_CODE}: days normally come from the biometric attendance; "
                        "this upload will override them.")
    if locked:
        errors.append(_locked_message(month_start))
    return errors, warnings


# --- preview -----------------------------------------------------------------------------

def _employee_dict(result: MatchResult) -> Optional[dict]:
    if result.employee_id is None:
        return None
    return {"id": result.employee_id, "code": result.employee_code, "name": result.employee_name}


def preview(db: Session, kind: str, file_bytes: bytes, month: str) -> dict[str, Any]:
    kind = parse_kind(kind)
    month_start, month_end = parse_month(month)
    sheet_name, sheet_rows = read_sheet(file_bytes, kind, month_start)

    ctx: dict[str, Any] = {"leave_codes": active_leave_codes(db) if kind == KIND_MUSTER else None,
                           "unchecked_leave_codes": set()}
    parser = PARSERS[kind]
    parsed: list[tuple[SheetRow, ParsedRow]] = []
    blank_rows = 0
    for row in sheet_rows:
        result = parser(row, month_start, month_end, ctx)
        if result is None:
            blank_rows += 1
            continue
        if row.excel_id is None and row.excel_name is None:
            result.errors.insert(0, "No Employee ID or name on this row.")
        parsed.append((row, result))
    if not parsed:
        raise AttendanceImportError("No attendance data was found in the sheet - fill in at least one row.")

    matcher = EmployeeMatcher.from_db(db)
    matches: list[MatchResult] = []
    cache: dict[tuple, MatchResult] = {}
    for row, _ in parsed:
        if row.excel_id is None and row.excel_name is None:
            matches.append(MatchResult(UNMATCHED))
        elif kind == KIND_DAILY:
            key = (row.excel_id, row.excel_name)
            if key not in cache:
                cache[key] = matcher.match(row.excel_id, row.excel_name)
            matches.append(cache[key])
        else:
            matches.append(matcher.match(row.excel_id, row.excel_name))
    if kind != KIND_DAILY:
        mark_duplicates([("sheet", row.row_number, m) for (row, _), m in zip(parsed, matches)])

    matched_ids = {m.employee_id for m in matches if m.employee_id is not None}
    profiles = month_profiles(db, matched_ids, month_start, month_end) if kind == KIND_SITE_MONTHLY else {}
    locked = locked_employee_ids(db, matched_ids, month_start, month_end) if kind == KIND_SITE_MONTHLY else set()
    first_day_row: dict[tuple[int, str], int] = {}

    rows_out, batch_rows = [], []
    counts: Counter = Counter()
    for (row, result), match in zip(parsed, matches):
        employee_errors: list[str] = []
        employee_warnings: list[str] = []
        if match.employee_id is not None and kind == KIND_SITE_MONTHLY:
            employee_errors, employee_warnings = _site_employee_checks(
                profiles.get(match.employee_id), match.employee_id in locked, month_start)
            group = profiles.get(match.employee_id)
            result.data["pay_group"] = group[1].code if group else None
        if match.employee_id is not None and kind == KIND_DAILY and not result.errors:
            key = (match.employee_id, result.payload["work_date"])
            if key in first_day_row:
                employee_errors.append(f"Duplicate: {match.employee_code} {result.payload['work_date']} "
                                       f"is also in row {first_day_row[key]}.")
            else:
                first_day_row[key] = row.row_number
        errors = result.errors + employee_errors
        warnings = list(match.notes) + result.warnings + employee_warnings
        counts[match.match_status] += 1
        counts["errors"] += bool(errors)
        counts["warnings"] += bool(warnings)
        rows_out.append({
            "row_number": row.row_number,
            "excel_id": row.excel_id,
            "excel_name": row.excel_name,
            "match_status": match.match_status,
            "score": match.score,
            "employee": _employee_dict(match),
            "candidates": [c.as_dict() for c in match.candidates],
            "errors": errors,
            "employee_errors": employee_errors,
            "warnings": warnings,
            "data": result.data,
        })
        batch_rows.append({
            "row_number": row.row_number,
            "excel_id": row.excel_id,
            "excel_name": row.excel_name,
            "match_status": match.match_status,
            "employee_id": match.employee_id,
            "row_errors": result.errors,
            "employee_errors": employee_errors,
            "payload": result.payload,
        })

    warnings = []
    if ctx["unchecked_leave_codes"]:
        warnings.append("Leave types are not set up yet, so these leave codes were accepted without checking: "
                        + ", ".join(sorted(ctx["unchecked_leave_codes"])) + ".")
    summary = {
        "total": len(rows_out),
        "matched_by_id": counts[MATCHED_BY_ID],
        "matched_by_name": counts[MATCHED_BY_NAME],
        "ambiguous": counts[AMBIGUOUS],
        "unmatched": counts[UNMATCHED],
        "errors": counts["errors"],
        "warnings": counts["warnings"],
        "blank_rows": blank_rows,
    }
    return {
        "kind": kind,
        "month": f"{month_start:%Y-%m}",
        "month_start": month_start,
        "month_end": month_end,
        "sheet": sheet_name,
        "summary": summary,
        "warnings": warnings,
        "rows": rows_out,
        "_batch_rows": batch_rows,
    }


# --- commit ------------------------------------------------------------------------------

def _ref(row: dict) -> dict:
    return {"row_number": row["row_number"], "excel_id": row.get("excel_id"), "excel_name": row.get("excel_name")}


def _resolve_rows(db: Session, rows: list[dict], decisions: list[dict]) -> tuple[list, list]:
    by_number = {r["row_number"]: r for r in rows}
    decided: dict[int, dict] = {}
    for decision in decisions:
        number = decision.get("row_number")
        if number not in by_number:
            raise AttendanceImportError(f"Decision for unknown row {number}.")
        decided[number] = decision

    resolved, skipped = [], []
    for row in rows:
        decision = decided.get(row["row_number"])
        if decision and decision.get("skip"):
            skipped.append({**_ref(row), "reason": "skipped by reviewer"})
            continue
        if row["row_errors"]:
            skipped.append({**_ref(row), "reason": "row has errors: " + "; ".join(row["row_errors"])})
            continue
        employee_id = decision.get("employee_id") if decision else None
        if employee_id is None:
            if row["match_status"] not in (MATCHED_BY_ID, MATCHED_BY_NAME):
                status = "ambiguous" if row["match_status"] == AMBIGUOUS else "unmatched"
                skipped.append({**_ref(row), "reason": f"needs review ({status})"})
                continue
            if row["employee_errors"]:
                skipped.append({**_ref(row), "reason": "; ".join(row["employee_errors"])})
                continue
            employee_id = row["employee_id"]
        employee = db.get(Employee, employee_id)
        if employee is None:
            raise AttendanceImportError(f"Employee {employee_id} (row {row['row_number']}) does not exist.")
        resolved.append((row, employee))
    return resolved, skipped


def _one_row_per_employee(resolved: list) -> None:
    seen: dict[int, int] = {}
    clashes = []
    for row, employee in resolved:
        first = seen.setdefault(employee.id, row["row_number"])
        if first != row["row_number"]:
            clashes.append(f"{employee.employee_code}: rows {first} and {row['row_number']}")
    if clashes:
        raise AttendanceImportError("An employee can appear in one row only: " + "; ".join(clashes))


def _num(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _commit_daily(db, resolved, skipped, counts, batch_id, created_by, month_start, month_end):
    existing = active_overrides(db, month_start, month_end, [e.id for _, e in resolved])
    seen: dict[tuple[int, date], int] = {}
    committed = []
    for row, employee in resolved:
        payload = row["payload"]
        work_date = date.fromisoformat(payload["work_date"])
        key = (employee.id, work_date)
        if key in seen:
            skipped.append({**_ref(row), "reason": f"duplicate of row {seen[key]} for {employee.employee_code} "
                                                   f"on {work_date:%d-%m-%Y}"})
            continue
        seen[key] = row["row_number"]
        values = build_values(
            kind=KIND_TIMES, work_date=work_date,
            punch_in=time.fromisoformat(payload["punch_in"]) if payload["punch_in"] else None,
            punch_out=time.fromisoformat(payload["punch_out"]) if payload["punch_out"] else None,
            reason=payload["reason"],
        )
        _, outcome = save_override(db, employee.id, employee.employee_code, work_date, values,
                                   source=SOURCE_UPLOAD_DAILY, created_by=created_by, import_batch_id=batch_id,
                                   current=existing.get(key))
        counts[f"overrides_{outcome}"] += 1
        committed.append((row, employee))
    return committed


def _commit_muster(db, resolved, skipped, counts, batch_id, created_by, month_start, month_end):
    _one_row_per_employee(resolved)
    existing = active_overrides(db, month_start, month_end, [e.id for _, e in resolved])
    for row, employee in resolved:
        for day, (status, leave) in sorted(row["payload"]["days"].items(), key=lambda item: int(item[0])):
            work_date = month_start.replace(day=int(day))
            values = build_values(kind=KIND_STATUS, work_date=work_date, status_code=status,
                                  leave_type_code=leave, reason=MUSTER_REASON)
            _, outcome = save_override(db, employee.id, employee.employee_code, work_date, values,
                                       source=SOURCE_UPLOAD_MUSTER, created_by=created_by,
                                       import_batch_id=batch_id, current=existing.get((employee.id, work_date)))
            counts[f"overrides_{outcome}"] += 1
    return resolved


def _commit_site(db, resolved, skipped, counts, batch_id, created_by, month_start, month_end):
    _one_row_per_employee(resolved)
    employee_ids = [e.id for _, e in resolved]
    locked = locked_employee_ids(db, employee_ids, month_start, month_end)
    if locked:
        codes = sorted(e.employee_code for _, e in resolved if e.id in locked)
        raise AttendanceImportError(f"{_locked_message(month_start)} Employees: {', '.join(codes)}.", 409)
    profiles = month_profiles(db, employee_ids, month_start, month_end)
    committed = []
    for row, employee in resolved:
        if employee.id not in profiles:
            skipped.append({**_ref(row), "reason": f"{employee.employee_code} has no pay profile for {month_start:%Y-%m}"})
            continue
        payload = row["payload"]
        changes = {"present_days": payload["days_present"]}
        for source_key, column in (("ot_days", "overtime_days"), ("ot_hours", "overtime_hours"),
                                   ("lop_days", "lop_days")):
            if payload.get(source_key) is not None:
                changes[column] = payload[source_key]
        vi = find_input(db, employee.id, month_start, month_end)
        created = vi is None
        if created:
            vi = VariablePayInput(employee_id=employee.id, period_start=month_start, period_end=month_end,
                                  status="Draft", lop_days=0, paid_leave_days=0, overtime_hours=0,
                                  ad_hoc_earnings=[], ad_hoc_deductions=[])
            db.add(vi)
        elif all(_num(getattr(vi, column)) == _num(value) for column, value in changes.items()):
            counts["inputs_unchanged"] += 1
            committed.append((row, employee))
            continue
        for column, value in changes.items():
            setattr(vi, column, value)
        vi.source = INPUT_SOURCE
        vi.import_batch_id = batch_id
        counts["inputs_created" if created else "inputs_updated"] += 1
        committed.append((row, employee))
    return committed


COMMITTERS = {KIND_DAILY: _commit_daily, KIND_MUSTER: _commit_muster, KIND_SITE_MONTHLY: _commit_site}
COUNT_KEYS = ("overrides_created", "overrides_replaced", "overrides_unchanged",
              "inputs_created", "inputs_updated", "inputs_unchanged")


def commit(db: Session, kind: str, batch_id: str, meta: dict, rows: list[dict], decisions: list[dict],
           created_by: Optional[str]) -> dict[str, Any]:
    kind = parse_kind(kind)
    month_start, month_end = date.fromisoformat(meta["month_start"]), date.fromisoformat(meta["month_end"])
    resolved, skipped = _resolve_rows(db, rows, decisions)
    counts: Counter = Counter()
    try:
        committed = COMMITTERS[kind](db, resolved, skipped, counts, batch_id, created_by, month_start, month_end)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "kind": kind,
        "month": f"{month_start:%Y-%m}",
        "committed_rows": len(committed),
        **{key: counts.get(key, 0) for key in COUNT_KEYS},
        "rows": [{**_ref(row), "employee_id": e.id, "employee_code": e.employee_code,
                  "employee_name": employee_display_name(e)} for row, e in committed],
        "skipped": sorted(skipped, key=lambda s: s["row_number"]),
    }
