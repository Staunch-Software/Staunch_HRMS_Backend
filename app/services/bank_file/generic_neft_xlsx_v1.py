"""PLACEHOLDER bank file format — NOT verified against ICICI's actual bulk
disbursement template.

The company pays salaries via ICICI Bank, but the exact bulk-upload
template ICICI expects is not available yet. This module is a generic,
best-guess NEFT/RTGS layout used as a stand-in so the rest of the
disbursement pipeline (validation, persistence, download, history) can be
built and exercised end-to-end today. It is deliberately named
`generic_neft_xlsx_v1`, not `icici...`, so nobody mistakes it for a
verified ICICI-specific format.

Column layout, header labels, and serialization format live ONLY in this
module (registered under this format_key in registry.py) — business logic
in generate.py never hardcodes column order. Swapping in ICICI's real spec
later means: add a new module (e.g. `icici_actual_v1.py`, which may turn
out to be fixed-width rather than .xlsx) and one new registry.py entry —
nothing else changes.

DO NOT use the output of this formatter for a real salary disbursement
until it has been replaced with (or confirmed against) ICICI's actual
bulk-upload spec.
"""
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

# GUESS pending the real ICICI spec: in Indian banking, RTGS is conventionally
# used for large-value transfers and NEFT for smaller ones, with ₹2,00,000
# being a commonly-cited (but not universally fixed) cutover point. Confirm
# ICICI's actual threshold/rules before relying on this split for a real
# submission.
RTGS_THRESHOLD = Decimal("200000")

# GUESS pending the real ICICI spec: this exact 8-column order/labels are a
# generic NEFT/RTGS layout, not copied from any ICICI document (none was
# available). Replace this whole list (and the row-building loop below) once
# the real template is obtained.
COLUMNS = [
    "Beneficiary Name",
    "Beneficiary Account Number",
    "IFSC Code",
    "Amount",
    "Narration",
    "Debit Account Number",
    "Value Date",
    "Payment Mode",
]

SHEET_NAME = "Salary Disbursement"


def format_bank_file(beneficiaries: list[dict], debit_account: dict, run_metadata: dict) -> bytes:
    """Builds the in-memory .xlsx bank file.

    `beneficiaries` rows are expected to carry `beneficiary_name` already
    resolved by the caller to the bank account's `account_holder_name` —
    NOT the employee's display name. Banks reject/misroute a transfer where
    the beneficiary name doesn't match the account holder on record, so this
    distinction matters and must not be "fixed" back to the employee's name.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = SHEET_NAME
    ws.append(COLUMNS)
    for cell in ws[1]:
        cell.font = Font(bold=True)

    period_label = run_metadata.get("period_yyyymm", "")
    value_date = run_metadata.get("pay_date")
    debit_account_number = debit_account.get("debit_account_number", "")
    default_narration = f"SALARY {period_label}"

    for beneficiary in beneficiaries:
        amount = Decimal(str(beneficiary["amount"]))
        payment_mode = "RTGS" if amount > RTGS_THRESHOLD else "NEFT"
        narration = beneficiary.get("narration") or default_narration

        ws.append(
            [
                beneficiary["beneficiary_name"],
                beneficiary["account_number"],
                beneficiary["ifsc_code"],
                float(amount),
                narration,
                debit_account_number,
                value_date,
                payment_mode,
            ]
        )

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
