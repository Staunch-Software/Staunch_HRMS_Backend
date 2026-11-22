"""Pay-group administration used by the API: read views, create from blank /
template, duplicate, deactivate checks."""
from datetime import date
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.pay_group import EmployeePayProfile, PayGroup
from app.services.payroll_engine.formula.compiled import validate_definition
from app.services.payroll_engine.formula.definition import CODE_RE
from app.services.payroll_engine.formula.history import save_definition
from app.services.payroll_engine.formula.parser import Call, Name, walk
from app.services.payroll_engine.formula.references import _names
from app.services.payroll_engine.pay_groups import FORMULA, attendance_source_of, extract_params

PROFILE_FIELD_NAMES = {
    "MONTHLY_SALARY": "monthly_salary", "DAY_WAGE": "daily_wage", "SKILL": "skill_level",
    "PF_APPLICABLE": "pf_applicable", "ESI_APPLICABLE": "esi_applicable", "PF_CAP": "pf_cap",
    "FIXED_MONTHLY_AMOUNT": "fixed_monthly_amount",
}

STARTER_DEFINITION = {
    "standard_days": 30,
    "net_rounding": "ROUND",
    "tds_enabled": True,
    "attendance_source": "INPUT",
    "pf_applicable_default": False,
    "esi_applicable_default": False,
    "include_ad_hoc_inputs": True,
    "constants": [],
    "lookups": [],
    "employee_fields": [],
    "lines": [
        {"code": "BASIC", "name": "Basic", "kind": "EARNING", "formula": "MONTHLY_SALARY * PAID_DAYS / STANDARD_DAYS",
         "rounding": "ROUND", "decimals": 0, "show_on_payslip": True, "statutory_tag": "BASIC"},
    ],
}


def starter_definition(db: Session) -> dict:
    """STARTER_DEFINITION with the Rounding Rules defaults for net pay and lines."""
    from app.services.payroll_settings import rounding_defaults

    defaults = rounding_defaults(db)
    lines = [{**ln, "rounding": defaults["line_rounding"], "decimals": defaults["line_decimals"]}
             for ln in STARTER_DEFINITION["lines"]]
    return {**STARTER_DEFINITION, "net_rounding": defaults["net_rounding"], "lines": lines}


class AdminError(ValueError):
    def __init__(self, message: str, status_code: int = 400, detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail if detail is not None else message


def member_counts(db: Session, as_of: date) -> dict[int, int]:
    rows = (
        db.query(EmployeePayProfile.pay_group_id, func.count(func.distinct(EmployeePayProfile.employee_id)))
        .filter(
            EmployeePayProfile.effective_from <= as_of,
            (EmployeePayProfile.effective_to.is_(None)) | (EmployeePayProfile.effective_to >= as_of),
        )
        .group_by(EmployeePayProfile.pay_group_id)
        .all()
    )
    return dict(rows)


def open_profile_count(db: Session, group_id: int, as_of: date) -> int:
    """Profiles in force today or starting later (what deactivation would strand)."""
    return (
        db.query(func.count(func.distinct(EmployeePayProfile.employee_id)))
        .filter(
            EmployeePayProfile.pay_group_id == group_id,
            (EmployeePayProfile.effective_to.is_(None)) | (EmployeePayProfile.effective_to >= as_of),
        )
        .scalar()
    ) or 0


def definition_view(definition: Optional[dict]) -> dict:
    result = validate_definition(definition or {})
    defn = result.definition or {}
    names: set[str] = set()
    skill_tables: set[str] = set()
    for trees in result.parsed.values():
        names |= _names(trees)
        for tree in trees.values():
            for node in walk(tree):
                if (isinstance(node, Call) and node.name == "LOOKUP" and len(node.args) > 1
                        and isinstance(node.args[1], Name) and node.args[1].name == "SKILL"):
                    first = node.args[0]
                    skill_tables.add(first.name if isinstance(first, Name) else str(getattr(first, "value", "")).upper())
    skills = sorted({str(row.get("key")) for lk in defn.get("lookups", []) if lk.get("code") in skill_tables
                     for row in lk.get("rows", [])})
    return {
        "lines_count": len(defn.get("lines", [])),
        "profile_fields": [field for name, field in PROFILE_FIELD_NAMES.items() if name in names],
        "skill_levels": skills,
        "employee_fields": defn.get("employee_fields", []),
        "standard_days": float(defn["standard_days"]) if defn.get("standard_days") is not None else None,
    }


def effective_params(group: PayGroup) -> dict:
    if group.calc_model != FORMULA:
        return group.params or {}
    defn = group.definition or {}
    settings = {k: defn.get(k) for k in ("net_rounding", "tds_enabled", "pf_applicable_default",
                                          "esi_applicable_default", "standard_days") if defn.get(k) is not None}
    return {**extract_params(group), "tds_enabled": True, **settings, "attendance_source": attendance_source_of(group)}


def check_new_code(db: Session, code: str) -> str:
    code = (code or "").strip().upper()
    if not CODE_RE.match(code):
        raise AdminError("code must start with a letter and use only A-Z, 0-9 and _ (max 40 characters)")
    if db.query(PayGroup).filter(func.upper(PayGroup.code) == code).first():
        raise AdminError(f"A pay group with code {code} already exists", 409)
    return code


def create_group(db: Session, code: str, name: str, description: Optional[str], definition: dict,
                 user: Optional[str], note: Optional[str], is_active: bool = True) -> PayGroup:
    code = check_new_code(db, code)
    if not (name or "").strip():
        raise AdminError("name is required")
    result = validate_definition(definition)
    if not result.ok:
        raise AdminError("The definition has errors", 400,
                         {"message": "The definition has errors", "errors": result.errors, "warnings": result.warnings})
    group = PayGroup(code=code, name=name.strip(), description=description, calc_model=FORMULA, params={},
                     is_active=is_active, definition_version=0)
    save_definition(db, group, definition, user, note)
    return group
