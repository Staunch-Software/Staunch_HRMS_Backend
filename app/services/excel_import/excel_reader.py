"""Shared Excel-to-records reader for both upload modes. `pandas` +
`openpyxl` only — no client-side parsing (see module docstring in
payroll_run_uploads.py for why this is server-side).
"""
import math
from io import BytesIO

import pandas as pd


class ExcelParseError(Exception):
    """Raised for a workbook that cannot be parsed at all (wrong file type,
    corrupt file, no data rows, missing required columns) — distinct from a
    per-row validation error, which is reported in the preview instead of
    raising.
    """


def read_workbook_as_records(file_bytes: bytes) -> list[dict]:
    """Reads the first sheet of an .xlsx workbook and returns one dict per
    data row, keyed by (whitespace-trimmed) column header. Fully blank rows
    are dropped. NaN cells become None.
    """
    if not file_bytes:
        raise ExcelParseError("The uploaded file is empty.")
    try:
        df = pd.read_excel(BytesIO(file_bytes), engine="openpyxl", dtype=object)
    except Exception as exc:  # noqa: BLE001 - pandas/openpyxl raise many types for a bad file
        raise ExcelParseError(
            f"Could not read the uploaded file as an .xlsx workbook: {exc}"
        ) from exc

    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(how="all")

    def _clean_cell(value):
        if value is None:
            return None
        if isinstance(value, float) and math.isnan(value):
            return None
        return value

    records = []
    for row in df.to_dict(orient="records"):
        records.append({key: _clean_cell(value) for key, value in row.items()})
    return records
