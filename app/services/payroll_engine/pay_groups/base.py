"""Shared building blocks for pay-group calculation strategies.

A pay group encodes one location/client-contract payroll model. Every rate,
threshold, cap, divisor and skill table lives in `PayGroup.params` (HR edits
them as data when minimum wages are revised); strategy code only holds the
shape of the calculation.

Rounding mirrors Excel, because the models were ported from a workbook and
must reproduce it to the rupee:
  ROUND   -> half away from zero (Decimal ROUND_HALF_UP)
  ROUNDUP -> away from zero (Decimal ROUND_UP)
Python's round() (banker's rounding) is never used.

Monthly inputs come from the period's VariablePayInput row:
  present_days, lop_days, overtime_hours, overtime_days
  ad_hoc_earnings codes:  OTHER_PAYMENT, SUPERVISOR_ALLOWANCE, SPECIAL_ALLOWANCE,
                          PAID_LEAVE, WAGE_ARREAR, ADJUSTMENT
  ad_hoc_deductions codes: OTHER_ADVANCE, LOAN, EXCESS_WAGE_DEDUCTION
Ad-hoc items are paid/deducted as their own lines, outside every statutory
(PF/ESI/bonus) wage base, and before the group's net-pay rounding is applied.
Unrecognised codes are still paid/deducted (with a warning) so money entered
by HR is never silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation
from typing import Any, Optional

ZERO = Decimal("0")
CENT = Decimal("0.01")

EARNING = "earning"
DEDUCTION = "deduction"
EMPLOYER_COST = "employer_cost"
INFO = "info"

AD_HOC_EARNING_LABELS = {
    "OTHER_PAYMENT": "Other payment",
    "SUPERVISOR_ALLOWANCE": "Supervisor allowance",
    "SPECIAL_ALLOWANCE": "Special allowance",
    "PAID_LEAVE": "Paid leave",
    "WAGE_ARREAR": "Wage arrear",
    "ADJUSTMENT": "Adjustment",
}
AD_HOC_DEDUCTION_LABELS = {
    "OTHER_ADVANCE": "Other advance",
    "LOAN": "Loan",
    "EXCESS_WAGE_DEDUCTION": "Excess wage deduction",
}

NET_ROUNDING_CHOICES = ("NONE", "ROUND", "ROUNDUP")


class PayGroupCalculationError(ValueError):
    """Raised when an employee cannot be calculated (missing wage, unknown
    skill level, ...). The run records it as an Error line item."""


class PayGroupParamsError(ValueError):
    pass


def dec(value: Any, default: Decimal = ZERO) -> Decimal:
    if value is None or value == "":
        return default
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return default


def _clean(value: Decimal) -> Decimal:
    # Excel works to ~15 significant digits; collapse Decimal division residue
    # (e.g. 2238.5999999...97) before rounding so ROUNDUP does not overshoot.
    return value.quantize(Decimal("1e-9"), rounding=ROUND_HALF_UP)


def xl_round(value: Decimal, digits: int = 0) -> Decimal:
    return _clean(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_HALF_UP)


def xl_roundup(value: Decimal, digits: int = 0) -> Decimal:
    return _clean(value).quantize(Decimal(1).scaleb(-digits), rounding=ROUND_UP)


def q2(value: Decimal) -> Decimal:
    return _clean(value).quantize(CENT, rounding=ROUND_HALF_UP)


_SKILL_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def normalize_skill(value: Optional[str]) -> str:
    """'Highly-Skilled', 'Highly Skilled', 'highly skilled' -> 'HIGHLY_SKILLED'."""
    if not value:
        return ""
    return _SKILL_NON_ALNUM.sub("_", str(value).upper()).strip("_")


def skill_rate(table: dict, skill: Optional[str], param_name: str) -> Decimal:
    key = normalize_skill(skill)
    if key and key in table:
        return dec(table[key])
    if "DEFAULT" in table:
        return dec(table["DEFAULT"])
    raise PayGroupCalculationError(
        f"No '{param_name}' rate for skill level '{skill or '(blank)'}' "
        f"(configured: {', '.join(sorted(table)) or 'none'})."
    )


# --- params schema -----------------------------------------------------------

@dataclass(frozen=True)
class ParamField:
    kind: str  # number | skill_table | bool | choice
    description: str
    choices: tuple = ()


COMMON_PARAM_SPEC: dict[str, ParamField] = {
    "pf_applicable_default": ParamField(
        "bool", "PF applies when neither the pay profile nor the employee record says otherwise"
    ),
    "esi_applicable_default": ParamField(
        "bool", "ESI applies when neither the pay profile nor the employee record says otherwise"
    ),
    "net_rounding": ParamField(
        "choice", "Rounding applied to the final net pay (Excel ROUND / ROUNDUP / none)", NET_ROUNDING_CHOICES
    ),
}


def validate_param_values(spec: dict[str, ParamField], params: Any) -> list[str]:
    if not isinstance(params, dict):
        return ["params must be a JSON object"]
    errors: list[str] = []
    missing = sorted(set(spec) - set(params))
    unknown = sorted(set(params) - set(spec))
    if missing:
        errors.append(f"missing keys: {', '.join(missing)}")
    if unknown:
        errors.append(f"unknown keys: {', '.join(unknown)}")
    for key, fld in spec.items():
        if key not in params:
            continue
        value = params[key]
        if fld.kind == "number":
            if not _is_number(value) or dec(value) < 0:
                errors.append(f"'{key}' must be a non-negative number")
        elif fld.kind == "bool":
            if not isinstance(value, bool):
                errors.append(f"'{key}' must be true or false")
        elif fld.kind == "choice":
            if value not in fld.choices:
                errors.append(f"'{key}' must be one of {', '.join(fld.choices)}")
        elif fld.kind == "skill_table":
            if not isinstance(value, dict) or not value:
                errors.append(f"'{key}' must be a non-empty object of skill level -> rate")
                continue
            for skill, rate in value.items():
                if normalize_skill(skill) != skill:
                    errors.append(
                        f"'{key}' skill key '{skill}' must be upper-case with underscores "
                        f"(e.g. '{normalize_skill(skill) or 'DEFAULT'}')"
                    )
                if not _is_number(rate) or dec(rate) < 0:
                    errors.append(f"'{key}.{skill}' must be a non-negative number")
    return errors


def _is_number(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    if isinstance(value, str):
        try:
            Decimal(value)
            return True
        except InvalidOperation:
            return False
    return False


# --- inputs / outputs ----------------------------------------------------------

@dataclass
class PayProfileData:
    """Plain stand-in for EmployeePayProfile (same attribute names), so the
    strategies can run without a database (unit tests, Excel verification)."""

    monthly_salary: Any = None
    daily_wage: Any = None
    skill_level: Optional[str] = None
    pf_applicable: Optional[bool] = None
    esi_applicable: Optional[bool] = None
    pf_cap: Any = None
    fixed_monthly_amount: Any = None
    extra: dict = field(default_factory=dict)


@dataclass
class PayInputs:
    present_days: Optional[Decimal] = None
    lop_days: Decimal = ZERO
    overtime_hours: Decimal = ZERO
    overtime_days: Decimal = ZERO
    earnings: dict[str, Decimal] = field(default_factory=dict)
    deductions: dict[str, Decimal] = field(default_factory=dict)
    has_input_row: bool = True

    @classmethod
    def from_variable_input(cls, vi) -> "PayInputs":
        if vi is None:
            return cls(has_input_row=False)
        return cls(
            present_days=dec(vi.present_days) if vi.present_days is not None else None,
            lop_days=dec(vi.lop_days),
            overtime_hours=dec(vi.overtime_hours),
            overtime_days=dec(getattr(vi, "overtime_days", None)),
            earnings=_sum_items(vi.ad_hoc_earnings),
            deductions=_sum_items(vi.ad_hoc_deductions),
        )


def _sum_items(items) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for item in items or []:
        code = str(item.get("component_code") or "").strip().upper()
        if not code:
            continue
        totals[code] = totals.get(code, ZERO) + dec(item.get("amount"))
    return totals


@dataclass
class PayLine:
    code: str
    label: str
    kind: str
    amount: Decimal
    basis: Optional[str] = None


@dataclass
class PayResult:
    gross: Decimal
    total_deductions: Decimal
    net_pay: Decimal
    employer_cost: Decimal
    lines: list[PayLine]
    warnings: list[str]
    paid_days: Optional[Decimal] = None
    lop_days: Optional[Decimal] = None
    pf_wages: Optional[Decimal] = None
    esi_wages: Optional[Decimal] = None

    def line_amount(self, code: str) -> Decimal:
        return sum((ln.amount for ln in self.lines if ln.code == code), ZERO)

    def breakdown(self, calc_model: str) -> list[dict]:
        """Lines in the shape PayrollRunLineItem.computation_breakdown uses."""
        type_by_kind = {EARNING: "Earning", DEDUCTION: "Deduction", EMPLOYER_COST: "EmployerCost", INFO: "Info"}
        entries = [
            {
                "component_code": ln.code,
                "component_name": ln.label,
                "component_type": type_by_kind[ln.kind],
                "kind": ln.kind,
                "calculation_method": f"PayGroup:{calc_model}",
                "formula_or_value": ln.basis,
                "resolved_amount": float(q2(ln.amount)),
            }
            for ln in self.lines
        ]
        for warning in self.warnings:
            entries.append({
                "component_code": "_SYSTEM_NOTE",
                "component_name": "System Note",
                "component_type": "Info",
                "kind": INFO,
                "calculation_method": None,
                "formula_or_value": None,
                "resolved_amount": 0.0,
                "warning": warning,
            })
        return entries


class Ctx:
    """Per-calculation scratchpad handed to a strategy's `compute`."""

    def __init__(self, profile, employee, inputs: PayInputs, params: dict):
        self.profile = profile
        self.employee = employee
        self.inputs = inputs
        self.params = params
        self.lines: list[PayLine] = []
        self.warnings: list[str] = []
        self.paid_days: Optional[Decimal] = None
        self.lop_days: Optional[Decimal] = None
        self.pf_wages: Optional[Decimal] = None
        self.esi_wages: Optional[Decimal] = None
        self.consumed_earnings: set[str] = set()
        self.consumed_deductions: set[str] = set()

    def p(self, key: str) -> Decimal:
        return dec(self.params[key])

    def add(self, code: str, label: str, kind: str, amount: Decimal, basis: Optional[str] = None) -> Decimal:
        self.lines.append(PayLine(code, label, kind, amount, basis))
        return amount

    def earning(self, code, label, amount, basis=None) -> Decimal:
        return self.add(code, label, EARNING, amount, basis)

    def deduction(self, code, label, amount, basis=None) -> Decimal:
        return self.add(code, label, DEDUCTION, amount, basis)

    def employer(self, code, label, amount, basis=None) -> Decimal:
        return self.add(code, label, EMPLOYER_COST, amount, basis)

    def info(self, code, label, amount, basis=None) -> Decimal:
        return self.add(code, label, INFO, amount, basis)

    def flag(self, name: str) -> bool:
        """pf/esi applicability: pay profile override > employee record > group default."""
        value = getattr(self.profile, f"{name}_applicable", None)
        if value is None and self.employee is not None:
            value = getattr(self.employee, f"{name}_applicable", None)
        if value is None:
            value = bool(self.params[f"{name}_applicable_default"])
        return bool(value)

    def extra(self, key: str, default=None):
        extra = getattr(self.profile, "extra", None) or {}
        return extra.get(key, default)

    def require(self, value, what: str) -> Decimal:
        if value is None or value == "":
            raise PayGroupCalculationError(f"Pay profile has no {what}.")
        return dec(value)


class PayGroupStrategy:
    calc_model: str = ""
    description: str = ""
    PARAM_SPEC: dict[str, ParamField] = {}

    @classmethod
    def param_spec(cls) -> dict[str, ParamField]:
        return {**COMMON_PARAM_SPEC, **cls.PARAM_SPEC}

    # Each entry is a tuple of alternatives, at least one of which the pay
    # profile must set; "extra.<key>" refers to a key inside profile.extra.
    PROFILE_REQUIREMENTS: tuple[tuple[str, ...], ...] = ()
    # Whether the profile's skill_level must resolve in the skill tables.
    USES_SKILL_TABLES: bool = False

    @classmethod
    def validate_params(cls, params: Any) -> list[str]:
        return validate_param_values(cls.param_spec(), params)

    @classmethod
    def validate_profile(cls, profile, params: dict) -> list[str]:
        errors = []
        extra = getattr(profile, "extra", None) or {}
        for alternatives in cls.PROFILE_REQUIREMENTS:
            def present(name: str) -> bool:
                if name.startswith("extra."):
                    return extra.get(name[6:]) not in (None, "")
                return getattr(profile, name, None) not in (None, "")
            if not any(present(name) for name in alternatives):
                errors.append(f"{' or '.join(alternatives)} is required for {cls.calc_model}")
        if cls.USES_SKILL_TABLES and getattr(profile, "fixed_monthly_amount", None) in (None, ""):
            skill = normalize_skill(getattr(profile, "skill_level", None))
            for key, fld in cls.param_spec().items():
                table = params.get(key) if isinstance(params, dict) else None
                if fld.kind == "skill_table" and isinstance(table, dict) and "DEFAULT" not in table and skill not in table:
                    errors.append(
                        f"skill_level '{getattr(profile, 'skill_level', None) or ''}' has no rate in '{key}' "
                        f"(configured: {', '.join(sorted(table))})"
                    )
        return errors

    def compute(self, ctx: Ctx) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def calculate(self, profile, employee, inputs: PayInputs, params: dict) -> PayResult:
        errors = self.validate_params(params)
        if errors:
            raise PayGroupParamsError(f"Invalid params for {self.calc_model}: " + "; ".join(errors))
        ctx = Ctx(profile, employee, inputs, params)
        if not inputs.has_input_row:
            ctx.warnings.append(
                "No variable input found for this period — defaulted to full standard days "
                "(no LOP, no overtime). Enter variable inputs before calculating for accurate results."
            )
        self.compute(ctx)

        for code, amount in inputs.earnings.items():
            if code in ctx.consumed_earnings or amount == 0:
                continue
            label = AD_HOC_EARNING_LABELS.get(code)
            if label is None:
                label = code
                ctx.warnings.append(f"Unrecognised ad-hoc earning code '{code}' paid as a plain earning.")
            ctx.earning(code, label, amount, "monthly input")
        for code, amount in inputs.deductions.items():
            if code in ctx.consumed_deductions or amount == 0:
                continue
            label = AD_HOC_DEDUCTION_LABELS.get(code)
            if label is None:
                label = code
                ctx.warnings.append(f"Unrecognised ad-hoc deduction code '{code}' deducted as a plain deduction.")
            ctx.deduction(code, label, amount, "monthly input")

        return self._finalize(ctx)

    def _finalize(self, ctx: Ctx) -> PayResult:
        gross_raw = sum((ln.amount for ln in ctx.lines if ln.kind == EARNING), ZERO)
        ded_raw = sum((ln.amount for ln in ctx.lines if ln.kind == DEDUCTION), ZERO)
        employer = sum((ln.amount for ln in ctx.lines if ln.kind == EMPLOYER_COST), ZERO)
        net_raw = gross_raw - ded_raw

        mode = ctx.params["net_rounding"]
        if mode == "ROUND":
            net = xl_round(net_raw)
        elif mode == "ROUNDUP":
            net = xl_roundup(net_raw)
        else:
            net = q2(net_raw)

        gross = q2(gross_raw)
        deductions = q2(ded_raw)
        round_off = net - (gross - deductions)
        if round_off != 0:
            ctx.earning("ROUND_OFF", "Net pay round-off", round_off, mode)
            gross += round_off

        return PayResult(
            gross=gross,
            total_deductions=deductions,
            net_pay=net,
            employer_cost=q2(employer),
            lines=ctx.lines,
            warnings=ctx.warnings,
            paid_days=ctx.paid_days,
            lop_days=ctx.lop_days,
            pf_wages=q2(ctx.pf_wages) if ctx.pf_wages is not None else None,
            esi_wages=q2(ctx.esi_wages) if ctx.esi_wages is not None else None,
        )
