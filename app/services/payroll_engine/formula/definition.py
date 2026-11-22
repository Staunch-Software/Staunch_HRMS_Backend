"""Pay-group formula definition: shape checks and normalisation. See
references.py for formula parsing / reference checks and validate.py for the
single entry point the API uses."""
import copy
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.payroll_engine.formula import catalog as cat

CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$")
TOP_KEYS = {
    "version", "standard_days", "constants", "lookups", "employee_fields", "lines", "net_rounding",
    "include_ad_hoc_inputs", "tds_enabled", "attendance_source", "pf_applicable_default", "esi_applicable_default",
}
SETTING_DEFAULTS = {
    "net_rounding": "NONE", "include_ad_hoc_inputs": True, "tds_enabled": True, "attendance_source": "INPUT",
    "pf_applicable_default": False, "esi_applicable_default": False,
}
LINE_KEYS = {"code", "name", "kind", "formula", "rounding", "decimals", "show_on_payslip", "statutory_tag",
             "condition", "omit_if_zero", "description"}


def issue(path: str, message: str, line: str | None = None, position: int | None = None) -> dict:
    return {"path": path, "line": line, "position": position, "message": message}


def issue_text(item: dict) -> str:
    where = item["path"]
    if item.get("position"):
        where += f" (position {item['position']})"
    return f"{where}: {item['message']}"


def is_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    if isinstance(value, str):
        try:
            Decimal(value.strip())
            return value.strip() != ""
        except InvalidOperation:
            return False
    return False


def norm_code(value: Any) -> str:
    return str(value or "").strip().upper()


def lookup_key(value: Any) -> str:
    """Canonical lookup key: numbers as plain digits (5, 5.0, "5.00" -> "5"),
    text upper-case with underscores ('Highly Skilled' -> HIGHLY_SKILLED)."""
    from app.services.payroll_engine.pay_groups.base import normalize_skill
    if is_number(value):
        number = Decimal(str(value).strip()).normalize()
        return format(number, "f")
    return normalize_skill(str(value))


def _bool_setting(defn: dict, key: str, errors: list) -> None:
    if not isinstance(defn.get(key), bool):
        errors.append(issue(key, "must be true or false"))


def normalize(raw: Any) -> tuple[dict, list[dict]]:
    """A deep copy with codes upper-cased and defaults filled, plus shape errors."""
    errors: list[dict] = []
    if not isinstance(raw, dict):
        return {}, [issue("definition", "must be a JSON object")]
    defn = copy.deepcopy(raw)
    for key in sorted(set(defn) - TOP_KEYS):
        errors.append(issue(key, "unknown setting"))
    for key, value in SETTING_DEFAULTS.items():
        if defn.get(key) is None:
            defn[key] = value
    if not is_number(defn.get("standard_days")) or Decimal(str(defn["standard_days"])) <= 0:
        errors.append(issue("standard_days", "must be a number greater than 0"))
    if defn["net_rounding"] not in cat.NET_ROUNDINGS:
        errors.append(issue("net_rounding", f"must be one of {', '.join(cat.NET_ROUNDINGS)}"))
    if defn["attendance_source"] not in cat.ATTENDANCE_SOURCE_CHOICES:
        errors.append(issue("attendance_source", f"must be one of {', '.join(cat.ATTENDANCE_SOURCE_CHOICES)}"))
    for key in ("include_ad_hoc_inputs", "tds_enabled", "pf_applicable_default", "esi_applicable_default"):
        _bool_setting(defn, key, errors)
    for key in ("constants", "lookups", "employee_fields", "lines"):
        if defn.get(key) is None:
            defn[key] = []
        if not isinstance(defn[key], list):
            errors.append(issue(key, "must be a list"))
            defn[key] = []
    _constants(defn["constants"], errors)
    _lookups(defn["lookups"], errors)
    _fields(defn["employee_fields"], errors)
    _lines(defn["lines"], errors)
    return defn, errors


def _entry(items: list, i: int, path: str, errors: list) -> dict | None:
    item = items[i]
    if not isinstance(item, dict):
        errors.append(issue(f"{path}[{i}]", "must be an object"))
        return None
    item["code"] = norm_code(item.get("code"))
    if not CODE_RE.match(item["code"]):
        errors.append(issue(f"{path}[{i}].code", "must start with a letter and use only A-Z, 0-9 and _ "
                                                  "(max 40 characters)", item["code"] or None))
    if not str(item.get("label") or item.get("name") or "").strip() and path != "lines":
        item["label"] = item["code"]
    return item


def _constants(items: list, errors: list) -> None:
    for i in range(len(items)):
        item = _entry(items, i, "constants", errors)
        if item is not None and not is_number(item.get("value")):
            errors.append(issue(f"constants[{i}].value", "must be a number", item["code"]))


def _lookups(items: list, errors: list) -> None:
    for i in range(len(items)):
        item = _entry(items, i, "lookups", errors)
        if item is None:
            continue
        item.setdefault("key_label", "Key")
        if item.get("default") is not None and not is_number(item["default"]):
            errors.append(issue(f"lookups[{i}].default", "must be a number or null", item["code"]))
        rows = item.get("rows")
        if rows is None:
            rows = item["rows"] = []
        if not isinstance(rows, list):
            errors.append(issue(f"lookups[{i}].rows", "must be a list", item["code"]))
            item["rows"] = []
            continue
        seen = set()
        for j, row in enumerate(rows):
            path = f"lookups[{i}].rows[{j}]"
            if not isinstance(row, dict) or str(row.get("key") if row.get("key") is not None else "").strip() == "":
                errors.append(issue(path, "needs a non-empty key", item["code"]))
                continue
            key = lookup_key(row["key"])
            if key in seen:
                errors.append(issue(path, f"duplicate key '{row['key']}'", item["code"]))
            seen.add(key)
            if not is_number(row.get("value")):
                errors.append(issue(f"{path}.value", "must be a number", item["code"]))


def _fields(items: list, errors: list) -> None:
    for i in range(len(items)):
        item = _entry(items, i, "employee_fields", errors)
        if item is None:
            continue
        path = f"employee_fields[{i}]"
        item.setdefault("type", "number")
        item["required"] = bool(item.get("required", False))
        if item["type"] not in cat.FIELD_TYPES:
            errors.append(issue(f"{path}.type", f"must be one of {', '.join(cat.FIELD_TYPES)}", item["code"]))
            continue
        options = item.get("options")
        if item["type"] == "select":
            if not isinstance(options, list) or not options or not all(isinstance(o, str) and o for o in options):
                errors.append(issue(f"{path}.options", "a select field needs a list of text options", item["code"]))
        if item.get("key") is not None and not str(item["key"]).strip():
            errors.append(issue(f"{path}.key", "must be non-empty text when given", item["code"]))
        default = item.get("default")
        if default is not None:
            problem = field_value_error(item, default)
            if problem:
                errors.append(issue(f"{path}.default", problem, item["code"]))


def field_value_error(field: dict, value: Any) -> str | None:
    if field.get("type") == "number" and not is_number(value):
        return "must be a number"
    if field.get("type") == "select" and value not in (field.get("options") or []):
        return f"must be one of {', '.join(field.get('options') or [])}"
    if field.get("type") == "text" and not isinstance(value, str):
        return "must be text"
    return None


def _lines(items: list, errors: list) -> None:
    if not items:
        errors.append(issue("lines", "add at least one line"))
    for i in range(len(items)):
        item = _entry(items, i, "lines", errors)
        if item is None:
            continue
        path, code = f"lines[{i}]", item["code"]
        for key in sorted(set(item) - LINE_KEYS - {"label"}):
            errors.append(issue(f"{path}.{key}", "unknown line setting", code))
        if not str(item.get("name") or item.get("label") or "").strip():
            errors.append(issue(f"{path}.name", "is required", code))
        item["name"] = str(item.get("name") or item.pop("label", "") or "").strip()
        item.pop("label", None)
        item["kind"] = norm_code(item.get("kind"))
        if item["kind"] not in cat.LINE_KINDS:
            errors.append(issue(f"{path}.kind", f"must be one of {', '.join(cat.LINE_KINDS)}", code))
        if not isinstance(item.get("formula"), str) or not item["formula"].strip():
            errors.append(issue(f"{path}.formula", "is required", code))
        item["rounding"] = norm_code(item.get("rounding") or "NONE")
        if item["rounding"] not in cat.LINE_ROUNDINGS:
            errors.append(issue(f"{path}.rounding", f"must be one of {', '.join(cat.LINE_ROUNDINGS)}", code))
        if item.get("decimals") is None:
            item["decimals"] = 0
        if isinstance(item["decimals"], bool) or not isinstance(item["decimals"], int) or not 0 <= item["decimals"] <= 6:
            errors.append(issue(f"{path}.decimals", "must be a whole number from 0 to 6", code))
        if item.get("show_on_payslip") is None:
            item["show_on_payslip"] = item["kind"] != "INFO"
        item["omit_if_zero"] = bool(item.get("omit_if_zero", False))
        for key in ("show_on_payslip",):
            if not isinstance(item[key], bool):
                errors.append(issue(f"{path}.{key}", "must be true or false", code))
        tag = item.get("statutory_tag")
        item["statutory_tag"] = norm_code(tag) or None
        if item["statutory_tag"] and item["statutory_tag"] not in cat.STATUTORY_TAGS:
            errors.append(issue(f"{path}.statutory_tag", f"unknown tag '{tag}'", code))
        condition = item.get("condition")
        if condition is not None and not isinstance(condition, str):
            errors.append(issue(f"{path}.condition", "must be a formula (text)", code))
        item["condition"] = condition.strip() if isinstance(condition, str) and condition.strip() else None
