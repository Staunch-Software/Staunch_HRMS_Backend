"""validate_definition (all errors at once, for the API) and
compile_definition (parsed, ready to calculate; cached per definition)."""
import json
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any, Optional

from app.services.payroll_engine.formula import definition as d
from app.services.payroll_engine.formula import references as refs
from app.services.payroll_engine.pay_groups.base import PayGroupParamsError


@dataclass
class ValidationResult:
    definition: dict
    errors: list[dict]
    warnings: list[dict]
    parsed: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def error_texts(self) -> list[str]:
        return [d.issue_text(e) for e in self.errors]


def validate_definition(raw: Any) -> ValidationResult:
    defn, errors = d.normalize(raw)
    warnings: list[dict] = []
    if not defn:
        return ValidationResult(defn, errors, warnings)
    refs.check_names(defn, errors)
    parsed = refs.parse_lines(defn, errors)
    refs.check_references(defn, parsed, errors, warnings)
    return ValidationResult(defn, errors, warnings, parsed)


@dataclass(frozen=True)
class Lookup:
    code: str
    label: str
    key_label: str
    rows: dict  # normalised key -> Decimal
    default: Optional[Decimal]


@dataclass(frozen=True)
class Line:
    code: str
    name: str
    kind: str
    formula: str
    tree: Any
    condition: Optional[str]
    condition_tree: Any
    rounding: str
    decimals: int
    show_on_payslip: bool
    statutory_tag: Optional[str]
    omit_if_zero: bool


@dataclass(frozen=True)
class Compiled:
    definition: dict
    standard_days: Decimal
    constants: dict
    lookups: dict
    fields: dict  # code -> normalised field dict
    lines: tuple
    line_codes: frozenset
    settings: dict


def _dec(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def build(result: ValidationResult) -> Compiled:
    defn = result.definition
    lookups = {
        lk["code"]: Lookup(
            lk["code"], str(lk.get("label") or lk["code"]), str(lk.get("key_label") or "Key"),
            {d.lookup_key(row["key"]): _dec(row["value"]) for row in lk["rows"]},
            _dec(lk["default"]) if lk.get("default") is not None else None,
        )
        for lk in defn["lookups"]
    }
    lines = tuple(
        Line(ln["code"], ln["name"], ln["kind"], ln["formula"], result.parsed[i]["formula"], ln["condition"],
             result.parsed[i].get("condition"), ln["rounding"], ln["decimals"], ln["show_on_payslip"],
             ln["statutory_tag"], ln["omit_if_zero"])
        for i, ln in enumerate(defn["lines"])
    )
    return Compiled(
        definition=defn,
        standard_days=_dec(defn["standard_days"]),
        constants={c["code"]: _dec(c["value"]) for c in defn["constants"]},
        lookups=lookups,
        fields={f["code"]: f for f in defn["employee_fields"]},
        lines=lines,
        line_codes=frozenset(ln.code for ln in lines),
        settings={k: defn[k] for k in d.SETTING_DEFAULTS},
    )


@lru_cache(maxsize=256)
def _compile_text(text: str) -> Compiled:
    result = validate_definition(json.loads(text))
    if not result.ok:
        raise PayGroupParamsError("Invalid pay-group definition: " + "; ".join(result.error_texts()))
    return build(result)


def compile_definition(raw: Any) -> Compiled:
    try:
        text = json.dumps(raw, sort_keys=True, default=str)
    except (TypeError, ValueError) as exc:
        raise PayGroupParamsError(f"Invalid pay-group definition: {exc}") from None
    return _compile_text(text)
