"""Canonical employee-code normalization, shared by every place that has to
match an employee across sources (master Excel, payroll Excel, Matrix
attendance API, manual entry).

The same person shows up as "STEMP0005" (master, attendance), "ST-EMP-05"
(site payroll sheets) and occasionally with stray spaces/lowercase. All of
those normalize to "STEMP0005": letters upper-cased, separators dropped,
numeric suffix zero-padded to 4 digits.
"""
import re

_PREFIX_NUMBER = re.compile(r"^([A-Z]+)(\d+)$")


def normalize_employee_code(value) -> str:
    if value is None:
        return ""
    compact = re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()
    match = _PREFIX_NUMBER.match(compact)
    if not match:
        return compact
    prefix, number = match.groups()
    return f"{prefix}{int(number):04d}"
