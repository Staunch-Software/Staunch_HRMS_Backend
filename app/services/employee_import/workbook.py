"""Reads the employee master workbook: finds the expected sheets, maps
headers by name (loosely), and extracts one normalized record per data row.

Some sheets (notably "Relieved Employees") contain blocks of rows whose
columns don't line up with the sheet's own header row — pasted from other
sheets, or missing a column. Each row is therefore matched against a few
candidate layouts (the header layout, the standard master layout, and the
header layout with columns shifted from some point) and the layout whose
typed cells (dates, Yes/No, IFSC, PAN, phone...) parse best is used. The
header layout always wins unless another fits clearly better.
"""
import re
from dataclasses import dataclass, field
from datetime import date
from io import BytesIO
from typing import Any, Callable, Optional

from openpyxl import load_workbook

from app.services.employee_code import normalize_employee_code
from app.services.employee_import import values as v
from app.services.excel_import.excel_reader import ExcelParseError
from app.services.org_normalization import clean_label, smart_case

MAX_FILE_BYTES = 15 * 1024 * 1024
RELIEVED = "relieved"

# (sheet kind, display name, precedence rank — lower wins)
ACTIVE_SHEETS = [
    ("employee master", "Employee Master", 1),
    ("nats employee master", "NATS Employee Master", 2),
    ("nats", "NATS", 3),
]
STAFF_SHEET_RANK = 4
RELIEVED_RANK = 9


def _norm_header(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


EXACT_HEADERS = {
    "employeeid": "employee_code", "empcode": "employee_code", "employeecode": "employee_code",
    "empid": "employee_code", "empno": "employee_code",
    "employeename": "name", "name": "name", "empname": "name",
    "gender": "gender", "sex": "gender",
    "dateofbirth": "date_of_birth", "dob": "date_of_birth",
    "mobilenumber": "mobile_number", "mobile": "mobile_number", "mobileno": "mobile_number",
    "empmobile": "mobile_number", "phonenumber": "mobile_number",
    "personalemailid": "personal_email", "personalemail": "personal_email", "mailid": "personal_email",
    "emailid": "personal_email", "email": "personal_email",
    "department": "department", "designation": "designation",
    "dateofjoining": "date_of_joining", "doj": "date_of_joining",
    "worklocation": "work_location", "status": "status",
    "pfuannumber": "uan_number", "uannumber": "uan_number", "uan": "uan_number", "uanno": "uan_number",
    "esinumber": "esi_number", "esicno": "esi_number", "esino": "esi_number", "esicnumber": "esi_number",
    "bankname": "bank_name", "bank": "bank_name",
    "accountnumber": "account_number", "bankacno": "account_number", "accountno": "account_number",
    "bankaccountnumber": "account_number",
    "ifsccode": "ifsc_code", "ifsc": "ifsc_code",
    "highestqualification": "highest_qualification", "qualification": "highest_qualification",
    "yearofpassing": "year_of_passing",
    "enrollmentid": "enrollment_id", "contractno": "contract_no",
    "remarks": "remarks",
    "permanentaddress": "permanent_address", "address": "permanent_address",
    "aadhaarnumber": "aadhaar_number", "aadhaar": "aadhaar_number", "aadharnumber": "aadhaar_number",
    "aadhar": "aadhaar_number", "aadhaarno": "aadhaar_number",
    "pannumber": "pan_number", "panno": "pan_number", "pan": "pan_number",
    "relation": "relation",
    "emergencycontactnumber": "emergency_contact_number", "emergencycontact": "emergency_contact_number",
    "emergencycontactno": "emergency_contact_number",
    "bloodgroup": "blood_group",
    "geographiclocation": "geographic_location", "geo": "geographic_location",
    "client": "client", "clientbranch": "client_branch",
    "dol": "date_of_leaving", "dateofleaving": "date_of_leaving", "dateofrelieving": "date_of_leaving",
    "relievingdate": "date_of_leaving",
}
PREFIX_HEADERS = [
    ("employmenttype", "employment_type"),
    ("employeestatus", "status"),
    ("pfapplicable", "pf_applicable"),
    ("pfyesno", "pf_applicable"),
    ("esiapplicable", "esi_applicable"),
    ("esiyesno", "esi_applicable"),
    ("professional", "certifications"),
    ("certification", "certifications"),
    ("university", "university"),
    ("apprenticeshiptype", "apprenticeship_type"),
    ("contractstartdate", "contract_start_date"),
    ("contractenddate", "contract_end_date"),
    ("stipend", "stipend_amount"),
    ("stipund", "stipend_amount"),
    ("father", "father_spouse_name"),
    ("insurance", "insurance_applicable"),
]

# Column order of the standard master sheets (used for header-less blocks).
MASTER_LAYOUT = [
    None, "employee_code", "name", "gender", "date_of_birth", "mobile_number", "personal_email",
    "department", "designation", "employment_type", "date_of_joining", "work_location", "status",
    "pf_applicable", "uan_number", "esi_applicable", "esi_number", "bank_name", "account_number",
    "ifsc_code", "certifications", "highest_qualification", "year_of_passing", "university",
    "apprenticeship_type", "enrollment_id", "contract_no", "contract_start_date", "contract_end_date",
    "stipend_amount", "remarks", "permanent_address", "aadhaar_number", "pan_number",
    "father_spouse_name", "relation", "emergency_contact_number", "blood_group",
    "insurance_applicable", "geographic_location", "client", "client_branch",
]

FIELD_PARSERS: dict[str, Callable[[Any], tuple]] = {
    "name": v.parse_person_name,
    "gender": v.parse_gender,
    "date_of_birth": v.parse_date,
    "mobile_number": v.parse_phone,
    "personal_email": v.parse_email,
    "department": v.parse_label,
    "designation": v.parse_label,
    "employment_type": v.parse_employment_type,
    "date_of_joining": v.parse_date,
    "work_location": v.parse_label,
    "status": v.parse_status,
    "pf_applicable": v.parse_yes_no,
    "uan_number": v.parse_uan,
    "esi_applicable": v.parse_yes_no,
    "esi_number": v.parse_id_text,
    "bank_name": v.parse_label,
    "account_number": v.parse_account_number,
    "ifsc_code": v.parse_ifsc,
    "certifications": v.parse_free_text,
    "highest_qualification": v.parse_free_text,
    "year_of_passing": v.parse_year_text,
    "university": v.parse_free_text,
    "apprenticeship_type": v.parse_plain_text,
    "enrollment_id": v.parse_id_text,
    "contract_no": v.parse_id_text,
    "contract_start_date": v.parse_date,
    "contract_end_date": v.parse_date,
    "stipend_amount": v.parse_amount,
    "remarks": v.parse_free_text,
    "permanent_address": v.parse_free_text,
    "aadhaar_number": v.parse_aadhaar,
    "pan_number": v.parse_pan,
    "father_spouse_name": v.parse_person_name,
    "relation": v.parse_relation,
    "emergency_contact_number": v.parse_phone,
    "blood_group": v.parse_blood_group,
    "insurance_applicable": v.parse_yes_no,
    "geographic_location": v.parse_label,
    "client": v.parse_label,
    "client_branch": v.parse_label,
    "date_of_leaving": v.parse_date,
}

# Fields whose successful parse is real evidence that a column lines up.
TYPED_FIELDS = {
    "gender", "date_of_birth", "mobile_number", "personal_email", "date_of_joining", "status",
    "pf_applicable", "uan_number", "esi_applicable", "account_number", "ifsc_code",
    "contract_start_date", "contract_end_date", "stipend_amount", "aadhaar_number", "pan_number",
    "emergency_contact_number", "blood_group", "insurance_applicable", "date_of_leaving",
    "employment_type",
}

FIELD_LABELS = {
    "name": "Employee Name", "gender": "Gender", "date_of_birth": "Date of Birth",
    "mobile_number": "Mobile Number", "personal_email": "Personal Email", "department": "Department",
    "designation": "Designation", "employment_type": "Employment Type", "date_of_joining": "Date of Joining",
    "work_location": "Work Location", "status": "Employee Status", "pf_applicable": "PF Applicable",
    "uan_number": "PF UAN Number", "esi_applicable": "ESI Applicable", "esi_number": "ESI Number",
    "bank_name": "Bank Name", "account_number": "Account Number", "ifsc_code": "IFSC Code",
    "certifications": "Certifications", "highest_qualification": "Highest Qualification",
    "year_of_passing": "Year of Passing", "university": "University", "apprenticeship_type": "Apprenticeship Type",
    "enrollment_id": "Enrollment ID", "contract_no": "Contract No", "contract_start_date": "Contract Start Date",
    "contract_end_date": "Contract End Date", "stipend_amount": "Stipend Amount", "remarks": "Remarks",
    "permanent_address": "Permanent Address", "aadhaar_number": "Aadhaar Number", "pan_number": "PAN Number",
    "father_spouse_name": "Father's / Spouse Name", "relation": "Relation",
    "emergency_contact_number": "Emergency Contact Number", "blood_group": "Blood Group",
    "insurance_applicable": "Insurance", "geographic_location": "Geographic Location", "client": "Client",
    "client_branch": "Client Branch", "date_of_leaving": "Date of Leaving",
}

SMART_CASE_FIELDS = {"client", "client_branch", "geographic_location", "highest_qualification", "university"}


def match_header(value: Any) -> Optional[str]:
    key = _norm_header(value)
    if not key:
        return None
    if key in EXACT_HEADERS:
        return EXACT_HEADERS[key]
    for prefix, field_name in PREFIX_HEADERS:
        if key.startswith(prefix):
            return field_name
    return None


def classify_sheet(title: str) -> Optional[tuple[str, str, int]]:
    """Returns (kind, display_name, rank) for an expected sheet, else None."""
    key = re.sub(r"\s+", " ", title.strip().lower())
    for name, display, rank in ACTIVE_SHEETS:
        if key == name:
            return "active", display, rank
    if key.startswith("staff") and "intern" in key:
        return "active", "Staff & Internship Staffs", STAFF_SHEET_RANK
    if "relieved" in key:
        return RELIEVED, "Relieved Employees", RELIEVED_RANK
    return None


@dataclass
class SourceRecord:
    sheet: str
    sheet_kind: str
    rank: int
    row_number: int
    employee_code: str
    raw_code: Optional[str]
    fields: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class SkippedRow:
    sheet: str
    row_number: int
    name: Optional[str]
    reason: str


@dataclass
class ParsedWorkbook:
    records: list[SourceRecord]
    skipped: list[SkippedRow]
    sheets_used: list[str]


def _header_mapping(rows: list[tuple]) -> tuple[int, dict[int, str]]:
    best_index, best_map = -1, {}
    for index, row in enumerate(rows[:5]):
        mapping: dict[int, str] = {}
        for col, cell in enumerate(row):
            field_name = match_header(cell)
            if field_name and field_name not in mapping.values():
                mapping[col] = field_name
        if "employee_code" in mapping.values() and len(mapping) > len(best_map):
            best_index, best_map = index, mapping
    return best_index, best_map


class _RowScorer:
    def __init__(self, row: tuple):
        self.row = row
        self.cache: dict[tuple[int, str], int] = {}

    def cell(self, col: int, field_name: Optional[str]) -> int:
        if field_name is None or field_name == "employee_code" or col >= len(self.row):
            return 0
        key = (col, field_name)
        if key not in self.cache:
            raw = self.row[col]
            if v.is_blank(raw):
                score = 0
            else:
                value, warning = FIELD_PARSERS[field_name](raw)
                if value is None and warning:
                    score = -1
                elif field_name in TYPED_FIELDS and not warning:
                    score = 1
                else:
                    score = 0
            self.cache[key] = score
        return self.cache[key]

    def score(self, mapping: dict[int, Optional[str]]) -> int:
        return sum(self.cell(col, name) for col, name in mapping.items())


def _shifted(header_map: dict[int, str], start: int, delta: int, width: int) -> dict[int, str]:
    """Data column c holds the header column c - delta for c >= start."""
    mapping = {col: name for col, name in header_map.items() if col < start}
    for col in range(start, width):
        source = col - delta
        if source >= start and source in header_map:
            mapping[col] = header_map[source]
    return mapping


def _choose_layout(row: tuple, header_map: dict[int, str]) -> tuple[dict[int, str], Optional[str]]:
    scorer = _RowScorer(row)
    base_score = scorer.score(header_map)
    best_map, best_score, note = header_map, base_score, None
    width = max(len(row), max(header_map) + 1 if header_map else 0)

    master_map = {i: name for i, name in enumerate(MASTER_LAYOUT) if name}
    if master_map != header_map:
        score = scorer.score(master_map)
        if score > best_score + 2:
            best_map, best_score, note = master_map, score, "standard master column order"

    has_failures = any(scorer.cell(c, n) < 0 for c, n in header_map.items())
    if has_failures:
        for delta in (-2, -1, 1, 2):
            for start in range(2, width):
                candidate = _shifted(header_map, start, delta, width)
                score = scorer.score(candidate)
                if score > best_score + 2:
                    direction = "left" if delta < 0 else "right"
                    best_map, best_score = candidate, score
                    note = f"columns from {start + 1} onward shifted {abs(delta)} {direction}"
    return best_map, note


def _split_name(full_name: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    if not full_name:
        return None, None
    tokens = full_name.split(" ")
    if len(tokens) == 1:
        return tokens[0], None
    return " ".join(tokens[:-1]), tokens[-1]


def normalize_row(raw_fields: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Parses raw cell values into model-ready values; returns (fields,
    warnings). Blank/unparseable values are omitted from fields."""
    fields: dict[str, Any] = {}
    warnings: list[str] = []
    for field_name, raw in raw_fields.items():
        parser = FIELD_PARSERS.get(field_name)
        if parser is None or v.is_blank(raw):
            continue
        value, warning = parser(raw)
        if warning:
            suffix = " - ignored" if value is None else ""
            warnings.append(f"{FIELD_LABELS.get(field_name, field_name)}: {warning}{suffix}")
        if value is None:
            continue
        if field_name in SMART_CASE_FIELDS:
            value = smart_case(value)
        fields[field_name] = value
    dob = fields.get("date_of_birth")
    if dob and not (1930 <= dob.year <= date.today().year - 14):
        warnings.append(f"Date of Birth: implausible date ({dob.isoformat()}) - ignored")
        del fields["date_of_birth"]
    return fields, warnings


def read_master_workbook(file_bytes: bytes) -> ParsedWorkbook:
    if not file_bytes:
        raise ExcelParseError("The uploaded file is empty.")
    if len(file_bytes) > MAX_FILE_BYTES:
        raise ExcelParseError("The uploaded file is too large (max 15 MB).")
    try:
        workbook = load_workbook(BytesIO(file_bytes), data_only=True, read_only=True)
    except Exception as exc:  # noqa: BLE001 - openpyxl raises many types for a bad file
        raise ExcelParseError(f"Could not read the uploaded file as an .xlsx workbook: {exc}") from exc

    records: list[SourceRecord] = []
    skipped: list[SkippedRow] = []
    sheets_used: list[str] = []
    try:
        for worksheet in workbook.worksheets:
            classification = classify_sheet(worksheet.title)
            if not classification:
                continue
            kind, display, rank = classification
            rows = list(worksheet.iter_rows(values_only=True))
            header_index, header_map = _header_mapping(rows)
            if header_index < 0:
                continue
            sheets_used.append(display)
            for offset, row in enumerate(rows[header_index + 1:]):
                row_number = header_index + 2 + offset
                if not any(not v.is_blank(cell) for cell in row):
                    continue
                layout, layout_note = _choose_layout(row, header_map)
                raw_fields = {name: row[col] for col, name in layout.items() if col < len(row)}
                raw_code = v.text(raw_fields.pop("employee_code", None))
                code = normalize_employee_code(raw_code)
                name = clean_label(raw_fields.get("name"))
                if not code:
                    skipped.append(SkippedRow(display, row_number, name, "skipped: no employee ID"))
                    continue
                if kind == RELIEVED:
                    # Everyone on this sheet has left; its STATUS column holds
                    # unrelated categories, so it isn't read.
                    raw_fields.pop("status", None)
                fields, warnings = normalize_row(raw_fields)
                record = SourceRecord(
                    sheet=display, sheet_kind=kind, rank=rank, row_number=row_number,
                    employee_code=code, raw_code=raw_code, fields=fields, warnings=warnings,
                )
                if layout_note:
                    record.notes.append(f"Row columns did not match the sheet headers; read using {layout_note}.")
                first, last = _split_name(fields.get("name"))
                if first:
                    fields["first_name"], fields["last_name"] = first, last
                records.append(record)
    finally:
        workbook.close()

    if not sheets_used:
        expected = ", ".join(f"'{d}'" for _, d, _ in ACTIVE_SHEETS) + ", 'Staff & Internship Staffs', 'Relieved Employees'"
        raise ExcelParseError(
            f"This workbook has none of the expected employee master sheets ({expected}) "
            "with an Employee ID column."
        )
    return ParsedWorkbook(records=records, skipped=skipped, sheets_used=sheets_used)
