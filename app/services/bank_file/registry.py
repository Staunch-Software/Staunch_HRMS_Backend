"""Registry of bank file format_keys -> formatter callables.

Two entries:
- `generic_neft_xlsx_v1` (see that module's docstring — an unverified
  placeholder), kept available as a fallback/comparison.
- `icici_bulk_salary_txt_v1` — the REAL ICICI bulk salary format,
  transcribed from ICICI's Composite Pay API docs. See that module's
  docstring for the exact field layout and flagged ambiguities.

Business logic in generate.py never hardcodes a column layout or file
extension/media type — it calls `get_formatter`/`get_file_extension`/
`get_media_type` below — so adding another format later only touches this
file plus one new formatter module.
"""
from typing import Callable

from app.services.bank_file import generic_neft_xlsx_v1, icici_bulk_salary_txt_v1

# Each formatter has the signature:
#   (beneficiaries: list[dict], debit_account: dict, run_metadata: dict) -> bytes
BANK_FILE_FORMATS: dict[str, Callable[[list[dict], dict, dict], bytes]] = {
    "generic_neft_xlsx_v1": generic_neft_xlsx_v1.format_bank_file,
    "icici_bulk_salary_txt_v1": icici_bulk_salary_txt_v1.format_bank_file,
}

BANK_FILE_EXTENSIONS: dict[str, str] = {
    "generic_neft_xlsx_v1": ".xlsx",
    "icici_bulk_salary_txt_v1": ".txt",
}

BANK_FILE_MEDIA_TYPES: dict[str, str] = {
    "generic_neft_xlsx_v1": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "icici_bulk_salary_txt_v1": "text/plain",
}


def get_formatter(format_key: str) -> Callable[[list[dict], dict, dict], bytes]:
    formatter = BANK_FILE_FORMATS.get(format_key)
    if formatter is None:
        known = ", ".join(sorted(BANK_FILE_FORMATS))
        raise ValueError(f"Unknown bank file format_key '{format_key}'. Known formats: {known}.")
    return formatter


def get_file_extension(format_key: str) -> str:
    return BANK_FILE_EXTENSIONS.get(format_key, ".xlsx")


def get_media_type(format_key: str) -> str:
    return BANK_FILE_MEDIA_TYPES.get(format_key, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
