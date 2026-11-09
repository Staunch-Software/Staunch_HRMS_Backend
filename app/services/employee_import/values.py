"""Cell-value parsers for the employee master import. Each parser returns
(value, warning) where warning is None on success; an unparseable value
returns (None, message) so the row is still imported without that field.
"""
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.services.org_normalization import clean_label

IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
_DMY = re.compile(r"^(\d{1,2})[./\-](\d{1,2})[./\-](\d{2}|\d{4})$")
_ISO = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$")
_BLOOD = re.compile(r"^(A1B|A2B|AB|A1|A2|A|B|O)([+-]?)$")

NA_TOKENS = {"na", "n/a", "nil", "none", "-", "--", "notapplicable", "not applicable", "nill", "null"}
YES_TOKENS = {"yes", "y", "true", "1", "applicable"}
NO_TOKENS = {"no", "n", "false", "0"}
GENDERS = {"male": "Male", "m": "Male", "female": "Female", "f": "Female", "other": "Other", "transgender": "Other"}
STATUS_ACTIVE = {"active", "working", "current"}
STATUS_INACTIVE = {"resigned", "inactive", "relieved", "terminated", "absconded", "left"}
EMPLOYMENT_TYPES = {
    "permanent": "Permanent",
    "contract": "Contract",
    "contractor": "Contract",
    "apprentice": "Apprentice",
    "internship": "Internship",
    "intern": "Internship",
    "trainee": "Trainee",
    "probation": "Probation",
}


def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not clean_label(value))


def text(value: Any) -> Optional[str]:
    """Plain text; Excel integers/floats are rendered without a trailing .0."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    return clean_label(value)


def text_or_none(value: Any) -> Optional[str]:
    """Text, with NA-style placeholders treated as blank."""
    result = text(value)
    if result and result.lower() in NA_TOKENS:
        return None
    return result


def _expand_year(two_digit: int) -> int:
    year = 2000 + two_digit
    return year if year <= date.today().year + 5 else year - 100


def parse_date(value: Any) -> tuple[Optional[date], Optional[str]]:
    if is_blank(value):
        return None, None
    if isinstance(value, datetime):
        return value.date(), None
    if isinstance(value, date):
        return value, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if 10000 <= value <= 80000:  # Excel serial day number
            return (datetime(1899, 12, 30) + timedelta(days=int(value))).date(), None
        return None, f"not a date ({value!r})"
    raw = clean_label(value)
    iso = _ISO.match(raw)
    if iso:
        y, m, d = (int(g) for g in iso.groups())
        try:
            return date(y, m, d), None
        except ValueError:
            return None, f"not a valid date ({raw!r})"
    match = _DMY.match(raw.replace(" ", ""))
    if not match:
        return None, f"not a date ({raw!r})"
    first, second, year_text = match.groups()
    day, month = int(first), int(second)
    year = int(year_text) if len(year_text) == 4 else _expand_year(int(year_text))
    note = None
    if month > 12 and day <= 12:
        day, month = month, day
        note = f"read {raw!r} as month/day/year"
    try:
        return date(year, month, day), note
    except ValueError:
        return None, f"not a valid date ({raw!r})"


def parse_yes_no(value: Any) -> tuple[Optional[bool], Optional[str]]:
    if is_blank(value):
        return None, None
    if isinstance(value, bool):
        return value, None
    token = clean_label(str(value)).lower()
    if token in YES_TOKENS:
        return True, None
    if token in NO_TOKENS:
        return False, None
    if token in NA_TOKENS:
        return None, None
    return None, f"not a Yes/No value ({clean_label(str(value))!r})"


def parse_amount(value: Any) -> tuple[Optional[Decimal], Optional[str]]:
    """Numbers, "15k"/"12.5K", "Rs. 15,000"."""
    if is_blank(value):
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value)), None
    raw = clean_label(str(value))
    if raw.lower() in NA_TOKENS:
        return None, None
    compact = re.sub(r"(?i)^(rs\.?|inr|₹)", "", raw.replace(",", "").replace(" ", ""))
    match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)(k)?(/-)?", compact)
    if not match:
        return None, f"not an amount ({raw!r})"
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation:
        return None, f"not an amount ({raw!r})"
    if match.group(2):
        amount *= 1000
    return amount, None


def parse_gender(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    token = clean_label(str(value)).lower()
    if token in GENDERS:
        return GENDERS[token], None
    return None, f"unknown gender ({clean_label(str(value))!r})"


def parse_employment_type(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    raw = clean_label(str(value))
    if raw.lower() in EMPLOYMENT_TYPES:
        return EMPLOYMENT_TYPES[raw.lower()], None
    return raw.title(), f"unrecognised value ({raw!r}) - kept as-is"


def parse_status(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    token = clean_label(str(value)).lower()
    if token in STATUS_ACTIVE:
        return "Active", None
    if token in STATUS_INACTIVE:
        return "Inactive", None
    return None, f"unknown employee status ({clean_label(str(value))!r})"


def parse_blood_group(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    raw = clean_label(str(value))
    compact = raw.upper().replace(" ", "")
    for word, sign in (("POSITIVE", "+"), ("NEGATIVE", "-")):
        if compact.endswith(word):
            compact = compact[: -len(word)]
            if not compact.endswith(("+", "-")):
                compact += sign
    compact = re.sub(r"VE$", "", compact)
    if compact.startswith("0"):
        compact = "O" + compact[1:]
    match = _BLOOD.match(compact)
    if not match:
        return None, f"unknown blood group ({raw!r})"
    return match.group(1) + match.group(2), None


def parse_phone(value: Any) -> tuple[Optional[str], Optional[str]]:
    """One or more numbers ("98400 72979", "9xxxxxxxxx/9xxxxxxxxx") ->
    digits-only numbers joined by " / "."""
    if is_blank(value):
        return None, None
    raw = text(value)
    if raw.lower() in NA_TOKENS:
        return None, None
    numbers = []
    for part in re.split(r"[/,;]", raw):
        digits = re.sub(r"[^\d+]", "", part)
        if digits:
            numbers.append(digits)
    if not numbers or any(len(re.sub(r"\D", "", n)) < 10 or len(re.sub(r"\D", "", n)) > 13 for n in numbers):
        return None, f"not a phone number ({raw!r})"
    return " / ".join(numbers), None


def parse_email(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    raw = clean_label(str(value)).replace(" ", "")
    if raw.lower() in NA_TOKENS:
        return None, None
    if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", raw):
        return raw.lower(), None
    return None, f"not an email address ({raw!r})"


def parse_ifsc(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    raw = clean_label(str(value))
    compact = raw.upper().replace(" ", "")
    if IFSC_RE.match(compact):
        return compact, None
    token = re.match(r"[A-Z]{4}0[A-Z0-9]{6}", compact)
    if token and not re.match(r"[A-Z0-9]", compact[11:12] or " "):
        return token.group(0), None
    return None, f"invalid IFSC code ({raw!r})"


def parse_account_number(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    raw = text(value)
    if raw.lower() in NA_TOKENS:
        return None, None
    digits = raw.replace(" ", "").replace("-", "")
    if digits.isdigit() and 6 <= len(digits) <= 20:
        return digits, None
    return None, f"invalid account number ({raw!r})"


def parse_aadhaar(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    raw = text(value)
    if raw.lower() in NA_TOKENS:
        return None, None
    digits = raw.replace(" ", "").replace("-", "")
    if digits.isdigit() and len(digits) == 12:
        return digits, None
    if digits.isdigit() and 10 <= len(digits) <= 14:
        return digits, f"Aadhaar number should have 12 digits ({raw!r}) - kept as-is"
    return None, f"not an Aadhaar number ({raw!r})"


def parse_pan(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    raw = text(value)
    if raw.lower() in NA_TOKENS:
        return None, None
    compact = raw.upper().replace(" ", "")
    if PAN_RE.match(compact):
        return compact, None
    if re.fullmatch(r"[A-Z0-9]{8,12}", compact) and re.search(r"[A-Z]", compact) and re.search(r"\d", compact):
        return compact, f"PAN format looks wrong ({raw!r}) - kept as-is"
    return None, f"not a PAN ({raw!r})"


def parse_uan(value: Any) -> tuple[Optional[str], Optional[str]]:
    if is_blank(value):
        return None, None
    raw = text(value)
    if raw.lower() in NA_TOKENS or raw.lower() == "new":
        return None, None
    digits = raw.replace(" ", "")
    if digits.isdigit() and 10 <= len(digits) <= 12:
        return digits, None
    return None, f"not a UAN ({raw!r})"


def parse_id_text(value: Any) -> tuple[Optional[str], Optional[str]]:
    """ESI number, enrollment id, contract no: text, NA treated as blank."""
    result = text_or_none(value)
    if result and result.lower() == "new":
        return None, None
    return result, None


def parse_year_text(value: Any) -> tuple[Optional[str], Optional[str]]:
    result = text_or_none(value)
    if result and not re.search(r"(19|20)\d{2}", result):
        return None, f"not a year ({result!r})"
    return result, None


def parse_person_name(value: Any) -> tuple[Optional[str], Optional[str]]:
    result = text_or_none(value)
    if result and (re.search(r"\d|@", result) or result.lower() in YES_TOKENS | NO_TOKENS):
        return None, f"not a name ({result!r})"
    return result, None


def parse_relation(value: Any) -> tuple[Optional[str], Optional[str]]:
    result = text_or_none(value)
    if not result:
        return None, None
    if re.search(r"\d|@", result) or len(result) > 40:
        return None, f"not a relation ({result!r})"
    cleaned = result.strip(" ,.")
    return (cleaned.title() if cleaned else None), None


def parse_free_text(value: Any) -> tuple[Optional[str], Optional[str]]:
    return text_or_none(value), None


def parse_plain_text(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Text where "Not Applicable" is itself a meaningful answer."""
    return text(value), None


def parse_label(value: Any) -> tuple[Optional[str], Optional[str]]:
    """Department/designation/client: text that should not look like a date,
    number, email or bank code (used to catch shifted columns)."""
    result = text_or_none(value)
    if result and (
        _DMY.match(result) or result.replace(" ", "").isdigit() or "@" in result or IFSC_RE.match(result.upper())
    ):
        return None, f"unexpected value ({result!r})"
    return result, None
