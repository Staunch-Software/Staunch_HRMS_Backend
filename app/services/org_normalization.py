"""Normalization of free-text org labels (work location, department,
designation) coming from spreadsheets, plus state-code inference for work
locations (Professional Tax depends on it). Shared by the employee master
import and, later, the payroll-sheet import.
"""
import re
from typing import Optional

_ALNUM = re.compile(r"[^a-z0-9]")

# Keyed by lowercase alphanumerics only ("Chennai - HO" -> "chennaiho").
LOCATION_ALIASES = {
    "chennai": "Chennai",
    "chennaiho": "Chennai HO",
    "ho": "Chennai HO",
    "hybridchennai": "Chennai (Hybrid)",
    "chennaihybrid": "Chennai (Hybrid)",
    "kudankulam": "Kudankulam",
    "koodankulam": "Kudankulam",
    "kknpp": "Kudankulam",
    "kknp": "Kudankulam",
    "synergy": "Synergy",
    "tuticorin": "Tuticorin",
    "thoothukudi": "Tuticorin",
    "kolkotta": "Kolkata",
    "kolkata": "Kolkata",
}

# Canonical location name (lowercase) -> state code.
LOCATION_STATE_CODES = {
    "chennai": "TN",
    "chennai ho": "TN",
    "chennai (hybrid)": "TN",
    "kudankulam": "TN",
    "tuticorin": "TN",
    "perungudi": "TN",
    "kallakurichi": "TN",
    "hyderabad": "TG",
    "surat": "GJ",
    "bhavnagar": "GJ",
    "kolkata": "WB",
}

# Geographic-location column values (lowercase alphanumerics) -> state code.
GEO_STATE_CODES = {
    "tn": "TN",
    "tamilnadu": "TN",
    "kallakurichi": "TN",
    "telangana": "TG",
    "tg": "TG",
    "ap": "AP",
    "andhrapradesh": "AP",
    "gujarat": "GJ",
    "gj": "GJ",
    "kerala": "KL",
    "odisha": "OD",
    "orissa": "OD",
    "westbengal": "WB",
    "wb": "WB",
    "kolkotta": "WB",
    "kolkata": "WB",
    "karnataka": "KA",
    "maharashtra": "MH",
}


def clean_label(value) -> Optional[str]:
    """Trims, collapses internal whitespace, and replaces the Unicode
    replacement character left by mis-encoded dashes/apostrophes."""
    if value is None:
        return None
    text = str(value).replace("�", "-").replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def label_key(value) -> str:
    """Case/whitespace-insensitive comparison key."""
    text = clean_label(value)
    return text.lower() if text else ""


def smart_case(value) -> Optional[str]:
    """All-lowercase labels, and ALL-CAPS labels containing a real word
    (6+ letters, e.g. MAINTENANCE), are title-cased; acronyms (BD, HR, CMU,
    KKNPP, E&I) and mixed case are kept."""
    text = clean_label(value)
    if not text:
        return None
    letters = re.sub(r"[^A-Za-z]", "", text)
    if not letters:
        return text
    longest_word = max((len(w) for w in re.findall(r"[A-Za-z]+", text)), default=0)
    if letters.islower() or (letters.isupper() and longest_word >= 6):
        return text.title()
    return text


def normalize_work_location(value) -> Optional[str]:
    text = clean_label(value)
    if not text:
        return None
    key = _ALNUM.sub("", text.lower())
    if key in LOCATION_ALIASES:
        return LOCATION_ALIASES[key]
    if "kknp" in key or "kudankulam" in key:
        return "Kudankulam"
    return smart_case(text)


def state_code_for_geo(geographic_location) -> Optional[str]:
    text = clean_label(geographic_location)
    if not text:
        return None
    return GEO_STATE_CODES.get(_ALNUM.sub("", text.lower()))


def infer_state_code(location_name, geographic_location=None) -> Optional[str]:
    """State code from the (normalized) location name, else from the row's
    Geographic Location, else None."""
    name = normalize_work_location(location_name)
    if name:
        code = LOCATION_STATE_CODES.get(name.lower())
        if code:
            return code
    return state_code_for_geo(geographic_location)


def department_code_base(name) -> str:
    """Uppercase alphanumerics of the name, capped to the column width."""
    code = re.sub(r"[^A-Z0-9]", "", (clean_label(name) or "").upper())
    return (code or "DEPT")[:30]


def unique_department_code(name, taken: set) -> str:
    base = department_code_base(name)
    if base not in taken:
        return base
    suffix = 2
    while True:
        tail = str(suffix)
        candidate = f"{base[: 30 - len(tail)]}{tail}"
        if candidate not in taken:
            return candidate
        suffix += 1
