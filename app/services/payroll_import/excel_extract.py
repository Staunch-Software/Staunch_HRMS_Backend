"""Reads the monthly payroll workbook (one sheet per pay group) into pay
profiles + monthly inputs, the same way for the HR import and for the
golden verification script (scripts/verify_pay_groups_against_excel.py), so
the two can never diverge.

Per row it captures what the pay-group strategy needs: per-employee figures
(pay profile), the month's attendance / ad-hoc amounts (PayInputs), and
Excel's own computed gross / PF / net for comparison. Row quirks handled:
per-employee PF caps written into the PF formula, fixed monthly amounts
typed into an Amount cell, "+constant" manual adjustments on a total
formula, Bhavnagar PF/ESI applicability from formula presence, and
repeated header rows (rows without a numeric serial in column A are not
data rows).
"""
from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Optional

import openpyxl
from openpyxl.utils import column_index_from_string, get_column_letter

from app.services.payroll_engine.pay_groups import PayInputs, PayProfileData
from app.services.payroll_engine.pay_groups.base import dec

_REF = re.compile(r"\$?([A-Z]{1,3})\$?(\d+)(?::\$?([A-Z]{1,3})\$?(\d+))?")


# --- workbook access -----------------------------------------------------------

class Sheet:
    def __init__(self, wb_formulas, wb_values, name: str, header_row: int, first_row: int):
        self.f = wb_formulas[name]
        self.v = wb_values[name]
        self.name = name
        self.header_row = header_row
        self.first_row = first_row

    def val(self, col: str, row: int):
        return self.v[f"{col}{row}"].value

    def num(self, col: str, row: int) -> Decimal:
        value = self.val(col, row)
        return dec(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else Decimal("0")

    def opt_num(self, col: str, row: int):
        value = self.val(col, row)
        return dec(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    def formula(self, col: str, row: int):
        return self.f[f"{col}{row}"].value

    def is_formula(self, col: str, row: int) -> bool:
        value = self.formula(col, row)
        return isinstance(value, str) and value.startswith("=") and bool(_REF.search(value[1:]))

    def yes(self, col: str, row: int) -> bool:
        return str(self.val(col, row) or "").strip().lower() == "yes"

    def text(self, col: str, row: int):
        value = self.val(col, row)
        return str(value).strip() if value is not None else None

    def header(self, col: str) -> str:
        value = self.f[f"{col}{self.header_row}"].value
        return " ".join(str(value).split()) if value else col

    def data_rows(self) -> list[int]:
        rows = []
        for r in range(self.first_row, self.f.max_row + 1):
            serial = self.val("A", r)
            if isinstance(serial, (int, float)) and not isinstance(serial, bool):
                rows.append(r)
        return rows


def pattern(sheet: Sheet, col: str, row: int) -> str:
    raw = sheet.formula(col, row)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return "EMPTY"
    if not sheet.is_formula(col, row):
        return "CONST"
    s = raw.upper().replace(" ", "").replace("$", "")
    s = re.sub(r"([A-Z]{1,3})" + str(row) + r"(?!\d)", r"\1{r}", s)
    s = re.sub(r"(?<![A-Z{])\d+(?:\.\d+)?", "{n}", s)
    while s.startswith("=(") and s.endswith(")") and _balanced(s[2:-1]):
        s = "=" + s[2:-1]
    return s


def _balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        depth += ch == "("
        depth -= ch == ")"
        if depth < 0:
            return False
    return depth == 0


def dominant_patterns(sheet: Sheet, rows: list[int]) -> dict[str, str]:
    result = {}
    for c in range(1, sheet.f.max_column + 1):
        col = get_column_letter(c)
        counts = Counter(pattern(sheet, col, r) for r in rows)
        counts.pop("EMPTY", None)
        if counts:
            top = counts.most_common(1)[0][0]
            if top not in ("CONST",):
                result[col] = top
    return result


def precedents(sheet: Sheet, col: str, row: int, seen=None) -> set[str]:
    """Same-row columns that feed `col`, following the row's own formulas."""
    seen = set() if seen is None else seen
    raw = sheet.formula(col, row)
    if not (isinstance(raw, str) and raw.startswith("=")):
        return seen
    for c1, r1, c2, r2 in _REF.findall(raw[1:].upper()):
        if int(r1) != row:
            continue
        lo = column_index_from_string(c1)
        hi = column_index_from_string(c2) if c2 else lo
        for idx in range(lo, hi + 1):
            ref = get_column_letter(idx)
            if ref not in seen:
                seen.add(ref)
                precedents(sheet, ref, row, seen)
    return seen


def overridden_columns(sheet: Sheet, row: int, net_col: str, dominant: dict[str, str]) -> list[str]:
    """Columns feeding Net Pay whose formula differs from the column's dominant pattern."""
    feeds_net = precedents(sheet, net_col, row) | {net_col}
    return [col for col, dom in dominant.items() if col in feeds_net and pattern(sheet, col, row) != dom]


def describe_pattern(pat: str) -> str:
    if pat == "CONST":
        return "typed as a constant"
    if pat == "EMPTY":
        return "left blank"
    return "formula altered"


def trailing_constant(formula) -> Decimal:
    """'=M3*F3+1000' -> 1000; '=ROUND(P5-V5-X5-Z5+1646,0)' -> 1646; else 0."""
    if not isinstance(formula, str):
        return Decimal("0")
    body = formula.replace(" ", "")
    m = re.search(r"([+-])(\d+(?:\.\d+)?),0\)$", body) or re.search(r"([+-])(\d+(?:\.\d+)?)$", body)
    if not m or not re.search(r"[A-Z]\d", body):
        return Decimal("0")
    amount = Decimal(m.group(2))
    return amount if m.group(1) == "+" else -amount


# --- per-sheet row mapping -------------------------------------------------------

@dataclass
class RowCase:
    profile: PayProfileData
    inputs: PayInputs
    excel_gross: Decimal
    excel_pf: Decimal
    excel_net: Decimal
    notes: list[str] = field(default_factory=list)


def _earn(**items) -> dict:
    return {k: v for k, v in items.items() if v}


def chennai(s: Sheet, r: int, params: dict) -> RowCase:
    cap_match = re.search(r"MIN\(AD\d+\*12%,(\d+(?:\.\d+)?)\)", str(s.formula("AJ", r)).replace(" ", ""))
    lop = s.num("Z", r)
    notes = []
    if s.opt_num("X", r) not in (None, Decimal("30")):
        notes.append(f"standard days {s.val('X', r)} differs from params")
    return RowCase(
        profile=PayProfileData(
            monthly_salary=s.num("F", r), esi_applicable=s.yes("G", r), pf_applicable=s.yes("H", r),
            pf_cap=Decimal(cap_match.group(1)) if cap_match else None,
        ),
        inputs=PayInputs(
            lop_days=lop, overtime_days=s.num("AA", r),
            earnings=_earn(OTHER_PAYMENT=s.num("AH", r)), deductions=_earn(OTHER_ADVANCE=s.num("AL", r)),
        ),
        excel_gross=s.num("AI", r), excel_pf=s.num("AJ", r), excel_net=s.num("AN", r), notes=notes,
    )


def tuticorin(s: Sheet, r: int, params: dict) -> RowCase:
    # The sheet's Per Day Salary already includes the group allowance; the
    # profile stores the employee's base rate.
    allowance = dec(params.get("per_day_allowance"))
    return RowCase(
        profile=PayProfileData(daily_wage=s.num("D", r) - allowance, pf_applicable=_pf_if_computed(s, "G", r)),
        inputs=PayInputs(present_days=s.num("E", r), earnings=_earn(SUPERVISOR_ALLOWANCE=s.num("J", r))),
        excel_gross=s.num("F", r) + s.num("J", r), excel_pf=s.num("G", r), excel_net=s.num("K", r),
    )


def hari(s: Sheet, r: int, params: dict) -> RowCase:
    return RowCase(
        profile=PayProfileData(daily_wage=s.num("G", r), skill_level=s.text("F", r), pf_applicable=s.yes("I", r),
                               esi_applicable=s.yes("J", r)),
        inputs=PayInputs(present_days=s.num("Y", r), overtime_hours=s.num("V", r),
                         earnings=_earn(WAGE_ARREAR=s.num("AF", r))),
        excel_gross=s.num("AH", r) + s.num("AE", r) + s.num("AG", r) + s.num("AF", r),
        excel_pf=s.num("AI", r), excel_net=s.num("AL", r),
    )


def _pf_if_computed(s: Sheet, col: str, r: int):
    """True when the sheet computes PF by formula on this row; otherwise None,
    leaving the employee record / group default in charge (a single month's
    typed-in PF is not evidence enough to switch PF off for good)."""
    return True if s.is_formula(col, r) else None


def _fixed_amount(s: Sheet, col: str, r: int):
    """An Amount cell holding a constant (or '=36800') is a fixed monthly amount."""
    if s.formula(col, r) is not None and not s.is_formula(col, r):
        return s.num(col, r)
    return None


def mcbs(s: Sheet, r: int, params: dict) -> RowCase:
    return RowCase(
        profile=PayProfileData(daily_wage=s.opt_num("D", r), skill_level=s.text("E", r),
                               fixed_monthly_amount=_fixed_amount(s, "I", r), pf_applicable=_pf_if_computed(s, "S", r)),
        inputs=PayInputs(
            present_days=s.num("H", r), overtime_hours=s.num("L", r),
            earnings=_earn(SPECIAL_ALLOWANCE=s.num("O", r)),
            deductions=_earn(LOAN=s.num("T", r), EXCESS_WAGE_DEDUCTION=s.num("P", r)),
        ),
        excel_gross=s.num("Q", r), excel_pf=s.num("S", r), excel_net=s.num("V", r),
    )


def cctv(s: Sheet, r: int, params: dict) -> RowCase:
    adjustment = trailing_constant(s.formula("N", r))
    notes = [f"manual {adjustment:+} in N ({s.header('N')}) entered as ADJUSTMENT"] if adjustment else []
    return RowCase(
        profile=PayProfileData(daily_wage=s.opt_num("D", r), skill_level=s.text("E", r),
                               fixed_monthly_amount=_fixed_amount(s, "G", r), pf_applicable=_pf_if_computed(s, "R", r)),
        inputs=PayInputs(
            present_days=s.num("F", r), overtime_hours=s.num("J", r),
            earnings=_earn(SPECIAL_ALLOWANCE=s.num("O", r), ADJUSTMENT=adjustment),
            deductions=_earn(LOAN=s.num("S", r)),
        ),
        excel_gross=s.num("P", r) + s.num("N", r), excel_pf=s.num("R", r), excel_net=s.num("U", r), notes=notes,
    )


def esfas(s: Sheet, r: int, params: dict) -> RowCase:
    return RowCase(
        profile=PayProfileData(daily_wage=s.opt_num("D", r), skill_level=s.text("E", r),
                               fixed_monthly_amount=_fixed_amount(s, "G", r), pf_applicable=_pf_if_computed(s, "S", r)),
        inputs=PayInputs(
            present_days=s.num("F", r), overtime_hours=s.num("J", r),
            earnings=_earn(PAID_LEAVE=s.num("O", r)), deductions=_earn(LOAN=s.num("T", r)),
        ),
        excel_gross=s.num("P", r) + s.num("N", r), excel_pf=s.num("S", r), excel_net=s.num("V", r),
    )


def bhavnagar(s: Sheet, r: int, params: dict) -> RowCase:
    adjustment = trailing_constant(s.formula("AB", r))
    notes = [f"manual {adjustment:+} in AB ({s.header('AB')}) entered as ADJUSTMENT"] if adjustment else []
    days = s.opt_num("K", r)
    return RowCase(
        profile=PayProfileData(daily_wage=s.num("G", r), skill_level=s.text("F", r),
                               pf_applicable=s.is_formula("V", r), esi_applicable=s.is_formula("X", r)),
        inputs=PayInputs(present_days=days, lop_days=s.num("J", r), earnings=_earn(ADJUSTMENT=adjustment)),
        excel_gross=s.num("P", r) + adjustment, excel_pf=s.num("V", r), excel_net=s.num("AB", r), notes=notes,
    )


@dataclass(frozen=True)
class SheetSpec:
    name: str  # sheet name in the reference workbook
    key: str  # normalized name without the month prefix, used to find the sheet in other months' files
    header_row: int
    first_row: int
    pay_group_code: str
    mapper: Callable[[Sheet, int, dict], RowCase]
    net_col: str
    name_col: str
    id_col: Optional[str]


SHEETS: tuple[SheetSpec, ...] = (
    SheetSpec("Sep Chn Salary", "CHN SALARY", 1, 2, "CHENNAI_HO", chennai, "AN", "C", "B"),
    SheetSpec("Sep Tuticorin", "TUTICORIN", 3, 5, "TUTICORIN", tuticorin, "K", "B", None),
    SheetSpec("HARI", "HARI", 3, 4, "KKNPP_HARI", hari, "AL", "C", "B"),
    SheetSpec("MCBS", "MCBS", 2, 3, "MCBS", mcbs, "V", "B", "C"),
    SheetSpec("CCTV", "CCTV", 2, 3, "CCTV", cctv, "U", "B", "C"),
    SheetSpec("ESFAS", "ESFAS", 2, 3, "ESFAS", esfas, "V", "B", "C"),
    SheetSpec("BhavNagar", "BHAVNAGAR", 3, 4, "BHAVNAGAR", bhavnagar, "AB", "C", "B"),
)

_MONTHS = {
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "SEPT", "OCT", "NOV", "DEC",
    "JANUARY", "FEBRUARY", "MARCH", "APRIL", "JUNE", "JULY", "AUGUST", "SEPTEMBER", "OCTOBER",
    "NOVEMBER", "DECEMBER",
}


def sheet_key(sheet_name: str) -> str:
    """'Sep Chn Salary' -> 'CHN SALARY', 'Oct-2026 Tuticorin' -> 'TUTICORIN'."""
    tokens = re.sub(r"[^A-Z0-9]+", " ", str(sheet_name).upper()).split()
    tokens = [t for t in tokens if t not in _MONTHS and not re.fullmatch(r"\d{2,4}", t)]
    return " ".join(tokens)


def find_sheet_name(sheet_names: list[str], spec: SheetSpec) -> Optional[str]:
    if spec.name in sheet_names:
        return spec.name
    for candidate in sheet_names:
        if sheet_key(candidate) == spec.key:
            return candidate
    return None


# --- whole-workbook extraction ----------------------------------------------------

@dataclass
class ExtractedRow:
    sheet: str
    row_number: int
    excel_name: Optional[str]
    excel_id: Optional[str]
    case: Optional[RowCase]
    error: Optional[str] = None
    # [(column, header, description)] of cells feeding Net Pay that break the column's formula pattern
    overrides: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class ExtractedSheet:
    spec: SheetSpec
    sheet_name: str
    rows: list[ExtractedRow]


def load_workbooks(source):
    """(formulas, cached values) workbooks from a path or raw bytes."""
    def _open(data_only: bool):
        if isinstance(source, (bytes, bytearray)):
            return openpyxl.load_workbook(io.BytesIO(source), data_only=data_only)
        return openpyxl.load_workbook(source, data_only=data_only)
    return _open(False), _open(True)


def _clean_text(value) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


def extract_sheet(wb_f, wb_v, spec: SheetSpec, params: dict, sheet_name: Optional[str] = None) -> ExtractedSheet:
    name = sheet_name or spec.name
    sheet = Sheet(wb_f, wb_v, name, spec.header_row, spec.first_row)
    rows = sheet.data_rows()
    dominant = dominant_patterns(sheet, rows)
    extracted = []
    for r in rows:
        excel_id = _clean_text(sheet.val(spec.id_col, r)) if spec.id_col else None
        item = ExtractedRow(sheet=name, row_number=r, excel_name=_clean_text(sheet.val(spec.name_col, r)),
                            excel_id=excel_id, case=None)
        try:
            item.case = spec.mapper(sheet, r, params)
        except Exception as exc:  # noqa: BLE001 - one bad row must not sink the sheet
            item.error = f"could not read row: {exc}"
        item.overrides = [
            (col, sheet.header(col), describe_pattern(pattern(sheet, col, r)))
            for col in overridden_columns(sheet, r, spec.net_col, dominant)
        ]
        extracted.append(item)
    return ExtractedSheet(spec=spec, sheet_name=name, rows=extracted)


def extract_workbook(source, params_by_code: dict[str, dict]) -> tuple[list[ExtractedSheet], list[str]]:
    """All known sheets present in the workbook, plus the names of the expected
    sheets that were not found."""
    wb_f, wb_v = load_workbooks(source)
    sheets, missing = [], []
    for spec in SHEETS:
        found = find_sheet_name(wb_f.sheetnames, spec)
        if found is None:
            missing.append(spec.name)
            continue
        sheets.append(extract_sheet(wb_f, wb_v, spec, params_by_code.get(spec.pay_group_code) or {}, found))
    return sheets, missing


# --- JSON shapes ---------------------------------------------------------------------

def _num(value) -> Optional[float]:
    return float(value) if value is not None else None


PROFILE_FIELDS = ("monthly_salary", "daily_wage", "skill_level", "pf_applicable", "esi_applicable", "pf_cap",
                  "fixed_monthly_amount")


def profile_to_dict(profile: PayProfileData) -> dict:
    out = {}
    for name in PROFILE_FIELDS:
        value = getattr(profile, name)
        out[name] = _num(value) if isinstance(value, (Decimal, int, float)) and not isinstance(value, bool) else value
    out["extra"] = dict(profile.extra or {})
    return out


def profile_from_dict(data: dict) -> PayProfileData:
    return PayProfileData(**{k: data.get(k) for k in PROFILE_FIELDS}, extra=dict(data.get("extra") or {}))


def inputs_to_dict(inputs: PayInputs) -> dict:
    return {
        "present_days": _num(inputs.present_days),
        "lop_days": _num(inputs.lop_days) or 0.0,
        "overtime_hours": _num(inputs.overtime_hours) or 0.0,
        "overtime_days": _num(inputs.overtime_days) or 0.0,
        "ad_hoc_earnings": [{"component_code": k, "amount": float(v)} for k, v in inputs.earnings.items()],
        "ad_hoc_deductions": [{"component_code": k, "amount": float(v)} for k, v in inputs.deductions.items()],
    }


def inputs_from_dict(data: dict) -> PayInputs:
    def total(items):
        out: dict[str, Decimal] = {}
        for item in items or []:
            out[item["component_code"]] = out.get(item["component_code"], Decimal("0")) + dec(item["amount"])
        return out
    present = data.get("present_days")
    return PayInputs(
        present_days=dec(present) if present is not None else None,
        lop_days=dec(data.get("lop_days")), overtime_hours=dec(data.get("overtime_hours")),
        overtime_days=dec(data.get("overtime_days")),
        earnings=total(data.get("ad_hoc_earnings")), deductions=total(data.get("ad_hoc_deductions")),
    )
