"""Employee master import: merges the parsed sheet records by employee code
(sheet precedence), compares them with the database, and applies the
result. The same planning code runs for preview (dry run, nothing written)
and for commit (re-planned against current DB state, then applied in one
transaction).
"""
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.employee import Employee, EmployeeBankAccount
from app.models.org import Department, Designation, WorkLocation
from app.services.employee_code import normalize_employee_code
from app.services.employee_import.workbook import RELIEVED, ParsedWorkbook, SkippedRow, SourceRecord
from app.services.org_normalization import (
    infer_state_code,
    label_key,
    normalize_work_location,
    smart_case,
    unique_department_code,
)

SCALAR_FIELDS = [
    "first_name", "last_name", "gender", "date_of_birth", "mobile_number", "personal_email",
    "employment_type", "date_of_joining", "date_of_leaving", "pf_applicable", "uan_number",
    "esi_applicable", "esi_number", "insurance_applicable", "certifications", "highest_qualification",
    "year_of_passing", "university", "apprenticeship_type", "enrollment_id", "contract_no",
    "contract_start_date", "contract_end_date", "stipend_amount", "remarks", "permanent_address",
    "aadhaar_number", "pan_number", "father_spouse_name", "relation", "emergency_contact_number",
    "blood_group", "geographic_location", "client", "client_branch",
]
MERGE_FIELDS = SCALAR_FIELDS + [
    "name", "status", "department", "designation", "work_location", "bank_name", "account_number", "ifsc_code",
]


# --- Org resolution -------------------------------------------------------------


class OrgResolver:
    """Case/whitespace-insensitive lookup of departments, designations and
    work locations, creating missing ones. In dry-run mode new objects are
    never added to the session (they only count towards the preview)."""

    def __init__(self, db: Session, dry_run: bool):
        self.db = db
        self.dry_run = dry_run
        departments = db.query(Department).all()
        self.departments = {label_key(d.name): d for d in departments}
        self.department_by_code = {d.code.upper(): d for d in departments}
        self.department_codes = set(self.department_by_code)
        dept_key_by_id = {d.id: label_key(d.name) for d in departments}
        self.designations = {
            (dept_key_by_id.get(d.department_id, ""), label_key(d.name)): d for d in db.query(Designation).all()
        }
        self.locations = {label_key(loc.name): loc for loc in db.query(WorkLocation).all()}
        self.created = {"departments": 0, "designations": 0, "work_locations": 0}

    def department(self, raw_name: Optional[str]) -> Optional[Department]:
        name = smart_case(raw_name)
        if not name:
            return None
        key = label_key(name)
        if key in self.departments:
            return self.departments[key]
        # An acronym that equals an existing department's code ("HR" ->
        # "Human Resources", code HR) is that department.
        acronym = re.sub(r"[^A-Za-z0-9]", "", name).upper()
        if acronym == name.upper().replace(" ", "") and acronym in self.department_by_code:
            department = self.department_by_code[acronym]
            self.departments[key] = department
            return department
        code = unique_department_code(name, self.department_codes)
        department = Department(name=name, code=code, status="Active")
        self.department_codes.add(code)
        self.departments[key] = department
        self.created["departments"] += 1
        if not self.dry_run:
            self.db.add(department)
            self.db.flush()
        return department

    def designation(self, raw_name: Optional[str], department: Optional[Department]) -> Optional[Designation]:
        name = smart_case(raw_name)
        if not name:
            return None
        dept_key = label_key(department.name) if department else ""
        key = (dept_key, label_key(name))
        if key in self.designations:
            return self.designations[key]
        designation = Designation(name=name, status="Active")
        self.designations[key] = designation
        self.created["designations"] += 1
        if not self.dry_run:
            designation.department_id = department.id if department else None
            self.db.add(designation)
            self.db.flush()
        return designation

    def location(self, raw_name: Optional[str], geographic_location: Optional[str]) -> Optional[WorkLocation]:
        name = normalize_work_location(raw_name)
        if not name:
            return None
        key = label_key(name)
        if key in self.locations:
            return self.locations[key]
        location = WorkLocation(name=name, state_code=infer_state_code(name, geographic_location), status="Active")
        self.locations[key] = location
        self.created["work_locations"] += 1
        if not self.dry_run:
            self.db.add(location)
            self.db.flush()
        return location


# --- Merging --------------------------------------------------------------------


@dataclass
class MergedEmployee:
    code: str
    primary: SourceRecord
    fields: dict[str, Any]
    status: str
    issues: list[dict] = field(default_factory=list)
    has_conflict: bool = False


def _issue(level: str, message: str) -> dict:
    return {"level": level, "message": message}


def merge_records(records: list[SourceRecord]) -> list[MergedEmployee]:
    by_code: dict[str, list[SourceRecord]] = {}
    for record in records:
        by_code.setdefault(record.employee_code, []).append(record)

    merged: list[MergedEmployee] = []
    for code, group in by_code.items():
        group.sort(key=lambda r: (r.rank, r.row_number))
        active = [r for r in group if r.sheet_kind != RELIEVED]
        relieved = [r for r in group if r.sheet_kind == RELIEVED]
        candidates = active or relieved
        primary = candidates[0]
        fields = dict(primary.fields)
        issues = [_issue("warning", w) for w in primary.warnings]
        issues += [_issue("info", n) for n in primary.notes]

        for other in candidates[1:]:
            filled = [k for k, value in other.fields.items() if k in MERGE_FIELDS and k not in fields]
            for key in filled:
                fields[key] = other.fields[key]
            if "name" in filled:
                fields["first_name"] = other.fields.get("first_name")
                fields["last_name"] = other.fields.get("last_name")
            where = f"'{other.sheet}' row {other.row_number}"
            if other.sheet == primary.sheet:
                message = f"Employee ID also appears in {where}; the first row was used"
            else:
                message = f"Also listed in {where}; '{primary.sheet}' takes precedence"
            if filled:
                message += f" ({len(filled)} blank field(s) filled from it)"
            issues.append(_issue("info", message + "."))

        has_conflict = bool(active and relieved)
        if has_conflict:
            rows = ", ".join(str(r.row_number) for r in relieved)
            issues.append(
                _issue(
                    "conflict",
                    f"Also listed in 'Relieved Employees' (row {rows}); the active-list record "
                    f"from '{primary.sheet}' was used and the employee stays Active.",
                )
            )
            status = fields.get("status") or "Active"
        elif active:
            status = fields.get("status") or "Active"
            if primary.fields.get("status") is None and any(i["message"].startswith("Employee Status") for i in issues):
                issues.append(_issue("warning", "Employee Status not recognised - imported as Active."))
        else:
            status = "Inactive"

        merged.append(MergedEmployee(code=code, primary=primary, fields=fields, status=status,
                                     issues=issues, has_conflict=has_conflict))
    merged.sort(key=lambda m: (m.primary.rank, m.primary.row_number))
    return merged


# --- Planning -------------------------------------------------------------------


@dataclass
class PlanItem:
    merged: MergedEmployee
    action: str  # new | update | unchanged | skipped
    employee: Optional[Employee]
    changes: dict[str, Any]
    department_name: Optional[str]
    designation_name: Optional[str]
    location_name: Optional[str]
    bank: Optional[dict]
    bank_change: Optional[str]  # create | update | None


def _same(current: Any, new: Any) -> bool:
    if current is None:
        return False
    if isinstance(new, Decimal) or isinstance(current, Decimal):
        try:
            return Decimal(str(current)) == Decimal(str(new))
        except Exception:  # noqa: BLE001
            return False
    if isinstance(current, date) and isinstance(new, date):
        return current == new
    return current == new


def _bank_from_fields(merged: MergedEmployee) -> Optional[dict]:
    fields = merged.fields
    bank_name, account, ifsc = fields.get("bank_name"), fields.get("account_number"), fields.get("ifsc_code")
    bank_warning = any(
        i["message"].startswith(("IFSC Code", "Account Number")) for i in merged.issues if i["level"] == "warning"
    )
    if bank_name and account and ifsc:
        holder = " ".join(p for p in (fields.get("first_name"), fields.get("last_name")) if p)
        return {"bank_name": bank_name, "account_number": account, "ifsc_code": ifsc, "account_holder_name": holder}
    if bank_name or account or ifsc or bank_warning:
        merged.issues.append(
            _issue("warning", "Bank details incomplete or invalid - bank account not imported (employee still imported).")
        )
    return None


def _primary_bank_account(employee: Employee) -> Optional[EmployeeBankAccount]:
    accounts = [a for a in employee.bank_accounts if a.is_active]
    return max(accounts, key=lambda a: a.id) if accounts else None


def build_plan(db: Session, parsed_records: list[SourceRecord], resolver: OrgResolver) -> list[PlanItem]:
    existing_by_code: dict[str, Employee] = {}
    for employee in db.query(Employee).order_by(Employee.id).all():
        existing_by_code.setdefault(normalize_employee_code(employee.employee_code), employee)

    plan: list[PlanItem] = []
    for merged in merge_records(parsed_records):
        fields = merged.fields
        employee = existing_by_code.get(merged.code)
        bank = _bank_from_fields(merged)

        if employee is None and not fields.get("first_name"):
            merged.issues.append(_issue("warning", "skipped: no employee name"))
            plan.append(PlanItem(merged, "skipped", None, {}, None, None, None, None, None))
            continue

        department = resolver.department(fields.get("department"))
        designation = resolver.designation(fields.get("designation"), department)
        location = resolver.location(fields.get("work_location"), fields.get("geographic_location"))

        changes: dict[str, Any] = {}
        for name in SCALAR_FIELDS:
            value = fields.get(name)
            if value is None:
                continue
            if employee is None or not _same(getattr(employee, name), value):
                changes[name] = value
        if employee is None or employee.status != merged.status:
            changes["status"] = merged.status
        if employee is not None and employee.employee_code != merged.code:
            changes["employee_code"] = merged.code
        for attr, obj in (("department_id", department), ("designation_id", designation),
                          ("work_location_id", location)):
            if obj is not None and (employee is None or obj.id is None or getattr(employee, attr) != obj.id):
                changes[attr] = obj

        bank_change = None
        if bank:
            current = _primary_bank_account(employee) if employee is not None else None
            if current is None:
                bank_change = "create"
            elif any(getattr(current, k) != bank[k] for k in ("bank_name", "account_number", "ifsc_code")):
                bank_change = "update"

        if employee is None:
            action = "new"
        elif changes or bank_change:
            action = "update"
        else:
            action = "unchanged"
        plan.append(PlanItem(
            merged, action, employee, changes,
            department.name if department else None,
            designation.name if designation else None,
            location.name if location else None,
            bank, bank_change,
        ))
    return plan


CHANGE_LABELS = {"department_id": "department", "designation_id": "designation", "work_location_id": "work_location"}


def preview(db: Session, parsed: ParsedWorkbook) -> dict:
    resolver = OrgResolver(db, dry_run=True)
    plan = build_plan(db, parsed.records, resolver)

    rows = []
    for item in plan:
        merged = item.merged
        name = merged.fields.get("name") or (item.employee and " ".join(
            p for p in (item.employee.first_name, item.employee.last_name) if p))
        rows.append({
            "employee_code": merged.code,
            "name": name,
            "source_sheet": merged.primary.sheet,
            "source_row": merged.primary.row_number,
            "action": item.action,
            "status": merged.status,
            "changed_fields": sorted(CHANGE_LABELS.get(k, k) for k in item.changes) if item.action == "update" else [],
            "bank_account": item.bank_change,
            "issues": merged.issues,
        })
    for skipped in parsed.skipped:
        rows.append({
            "employee_code": None,
            "name": skipped.name,
            "source_sheet": skipped.sheet,
            "source_row": skipped.row_number,
            "action": "skipped",
            "status": None,
            "changed_fields": [],
            "bank_account": None,
            "issues": [_issue("warning", skipped.reason)],
        })

    count = lambda action: sum(1 for i in plan if i.action == action)  # noqa: E731
    warning_rows = sum(1 for r in rows if any(i["level"] == "warning" for i in r["issues"]))
    summary = {
        "total_rows": len(rows),
        "new": count("new"),
        "updated": count("update"),
        "unchanged": count("unchanged"),
        "skipped_no_id": len(parsed.skipped),
        "skipped_other": count("skipped"),
        "conflicts": sum(1 for i in plan if i.merged.has_conflict),
        "warnings": warning_rows,
        "warning_messages": sum(1 for r in rows for i in r["issues"] if i["level"] == "warning"),
        "bank_accounts": sum(1 for i in plan if i.bank_change and i.action != "skipped"),
        "new_departments": resolver.created["departments"],
        "new_designations": resolver.created["designations"],
        "new_work_locations": resolver.created["work_locations"],
        "inactive": sum(1 for i in plan if i.action != "skipped" and i.merged.status == "Inactive"),
        "sheets_used": parsed.sheets_used,
    }
    return {"summary": summary, "rows": rows}


def commit(db: Session, parsed_records: list[SourceRecord]) -> dict:
    resolver = OrgResolver(db, dry_run=False)
    try:
        plan = build_plan(db, parsed_records, resolver)
        created = updated = unchanged = skipped = banks_created = banks_updated = 0
        for item in plan:
            if item.action == "skipped":
                skipped += 1
                continue
            if item.action == "unchanged":
                unchanged += 1
                continue
            employee = item.employee
            values = {}
            for key, value in item.changes.items():
                values[key] = value.id if key in CHANGE_LABELS else value
            if employee is None:
                employee = Employee(employee_code=item.merged.code, **values)
                db.add(employee)
                db.flush()
                created += 1
            else:
                for key, value in values.items():
                    setattr(employee, key, value)
                updated += 1

            if item.bank and item.bank_change:
                current = _primary_bank_account(employee) if item.action == "update" else None
                if current is None:
                    db.add(EmployeeBankAccount(employee_id=employee.id, is_primary=True, is_active=True, **item.bank))
                    banks_created += 1
                else:
                    for key, value in item.bank.items():
                        setattr(current, key, value)
                    banks_updated += 1
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "skipped": skipped,
        "bank_accounts_created": banks_created,
        "bank_accounts_updated": banks_updated,
        "departments_created": resolver.created["departments"],
        "designations_created": resolver.created["designations"],
        "work_locations_created": resolver.created["work_locations"],
    }


def records_to_batch(records: list[SourceRecord]) -> list[dict]:
    return [record.__dict__.copy() for record in records]


def records_from_batch(rows: list[dict]) -> list[SourceRecord]:
    return [SourceRecord(**row) for row in rows]


__all__ = ["preview", "commit", "records_to_batch", "records_from_batch", "SkippedRow"]
