"""The REAL ICICI Bank bulk salary disbursement file format, transcribed
from ICICI's "Composite Pay API v1.23" documentation
(backend/docs/icici/Composite_Pay_API_1.23.pdf, pages 59-61) and the
"PAB SAL File Upload Details" tab of
backend/docs/icici/Composite_API_ErrorCodes_1.23.xlsx.

IMPORTANT CORRECTION vs. the original task brief: the brief assumed this is
a FIXED-WIDTH file (values padded/truncated to the sheet's "Field Width"
column). That turned out to be wrong. The PDF (page 61) contains a
complete, real, base64-encoded sample FILE_CONTENT value. Decoding it gives:

    FHR|3|11/11/2020|BULKTEO|2|INR|000705000961|0011^
    MDR|000705000961|0011|SOURABH|2|INR|APITEST|ICIC0000011|WIB^
    MCW|000405011155|0011|SOURABH|1|INR|TestJUN|ICIC0000011|WIB^
    MCO|000451000301|0011|RAKESH|1|INR|OTHERTEST|NFT|DLXB0000092^

None of these values are padded to their sheet-listed widths (e.g.
BRANCH_ID is "0011", 4 characters, against a listed width of 9; PAYEE_NAME
is "SOURABH", 7 characters, against a listed width of 32). So this is a
plain variable-length, pipe-delimited format: fields are joined with "|",
and the sheet's "Field Width" column is a MAXIMUM length used for
validation only, not a pad target. This module follows the real decoded
sample, not the sheet's Field Width/Justification/Pad Character columns
literally — see the field-width validation below, which truncates only
free-text fields (name/remarks) and raises for anything else that would
exceed its documented max length.

Each line is terminated with "^" followed by "\n" (confirmed by the sample
above). One file is: one FHR header line, exactly one MDR line (the
single debit/remitter record — re-verified from the sheet: MDR is NOT
repeated per beneficiary), then one MCW or MCO line per beneficiary
payment (MCW if the beneficiary also banks with ICICI — IFSC starts with
"ICIC" — else MCO, routed via NEFT).

FLAGGED AMBIGUITY (confirm with ICICI RM): the sheet's Remarks column for
BRANCH_ID_CRW says 'Client need to pass last 4 digit for Bene A/c number',
but the real decoded sample above uses the constant "0011" for BRANCH_ID_CRW
(ACCOUNT_ID_CRW is "000405011155", whose last 4 digits are "1155", not
"0011"). This module follows the real worked sample (constant "0011",
matching every other BRANCH_ID_* field in the spec) since it is concrete,
authoritative evidence of what ICICI's own system accepted — not the
possibly-stale sheet remark. Flagged here and in the project report for the
user's RM to confirm.

FLAGGED ASSUMPTION: AMOUNT_* fields are formatted with exactly 2 decimal
places (e.g. "84950.50"), unlike the sample's bare integers ("1", "2"),
because real salary amounts carry paise and the field is documented only
as numeric type "N" with no explicit decimal-formatting rule. Confirm with
the RM if ICICI expects a different amount format (e.g. no decimal point,
amount in paise).
"""
import base64
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

# Max total records (FHR is not counted; MDR + one line per beneficiary is)
# per file, per the PDF page 59: "How much record user can upload in a
# single file — 500 Records (This restriction should be built at client end
# for each file upload)." Confirmed against the sample: FHR's
# TOTAL_NO_OF_RECORDS=3 for a file with 1 MDR + 1 MCW + 1 MCO line = 3.
MAX_TOTAL_RECORDS = 500
# -> at most MAX_TOTAL_RECORDS - 1 beneficiary (MCW/MCO) lines, since the
# MDR line always takes one slot.
MAX_BENEFICIARIES_PER_FILE = MAX_TOTAL_RECORDS - 1

LINE_TERMINATOR = "^\n"

ICICI_IFSC_PREFIX = "ICIC"
ICICI_CONSTANT_BRANCH_ID = "0011"
ICICI_ROUTING_NUMBER = "ICIC0000011"
NETWORK_ID_WITHIN = "WIB"
NETWORK_ID_OTHER_BANK = "NFT"

# Field max-lengths from the "PAB SAL File Upload Details" sheet, used ONLY
# for validation (truncate free-text fields, raise for everything else) —
# see module docstring for why these are not pad targets.
FHR_WIDTHS = {
    "FILEHR": 6,
    "TOTAL_NO_OF_RECORDS": 5,
    "TRANSACTION_DATE": 10,
    "EXTERNAL_FILE_REFERENCE": 10,
    "TOTAL_AMOUNT": 15,
    "CURRENCY": 3,
    "TRACE_ACCOUNT_ID": 12,
    "TRACE_BRANCH_ID": 9,
}
MDR_WIDTHS = {
    "MDDRT": 6,
    "ACCOUNT_ID_DR": 12,
    "BRANCH_ID_DR": 9,
    "PAYEE_NAME_DR": 32,
    "AMOUNT_DR": 15,
    "CURRENCY_DR": 3,
    "REMARKS_DR": 30,
    "ROUTING_NUMBER_DR": 11,
    "NETWORK_ID_DR": 10,
}
MCO_WIDTHS = {
    "MDCROB": 6,
    "ACCOUNT_ID_CRO": 34,
    "BRANCH_ID_CRO": 9,
    "PAYEE_NAME_CRO": 32,
    "AMOUNT_CRO": 15,
    "CURRENCY_CRO": 3,
    "REMARKS_CRO": 30,
    "NETWORK_ID_CRO": 10,
    "ROUTING_NUMBER_CRO": 11,
}
MCW_WIDTHS = {
    "MDCRT": 6,
    "ACCOUNT_ID_CRW": 12,
    "BRANCH_ID_CRW": 9,
    "PAYEE_NAME_CRW": 32,
    "AMOUNT_CRW": 15,
    "CURRENCY_CRW": 3,
    "REMARKS_CRW": 30,
    "ROUTING_NUMBER_CRW": 11,
    "NETWORK_ID_CRW": 10,
}

# Field names that are free text and safe to truncate (name/remarks). Every
# other field raises ValueError if it would exceed its documented max width
# — truncating an account number, IFSC/routing code, or amount could
# misroute real money, so that must fail loudly instead of silently
# corrupting the file.
TRUNCATABLE_FIELDS = {
    "PAYEE_NAME_DR", "REMARKS_DR",
    "PAYEE_NAME_CRO", "REMARKS_CRO",
    "PAYEE_NAME_CRW", "REMARKS_CRW",
    "EXTERNAL_FILE_REFERENCE",
}


class ICICIBulkFileError(ValueError):
    """Raised for anything that would produce an invalid/corrupt ICICI bulk
    file — an oversized non-truncatable field, or exceeding the 500-record
    file limit."""


def _field(value: str, field_name: str, widths: dict) -> str:
    value = "" if value is None else str(value)
    max_width = widths[field_name]
    if len(value) > max_width:
        if field_name in TRUNCATABLE_FIELDS:
            return value[:max_width]
        raise ICICIBulkFileError(
            f"{field_name} value '{value}' is {len(value)} characters, exceeding ICICI's documented "
            f"max width of {max_width} for this field. Refusing to truncate a non-free-text field."
        )
    return value


def _fmt_amount(amount) -> str:
    """2 decimal places, no thousands separator, no currency symbol — see
    the module docstring's flagged assumption on amount formatting."""
    quantized = Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{quantized:.2f}"


def _fmt_date(value) -> str:
    """MM/DD/YYYY, per the sheet and the real sample ('11/11/2020')."""
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%m/%d/%Y")


def is_within_icici(ifsc_code: str) -> bool:
    return bool(ifsc_code) and ifsc_code.strip().upper().startswith(ICICI_IFSC_PREFIX)


@dataclass
class ICICIBeneficiaryRecord:
    account_number: str
    ifsc_code: str
    beneficiary_name: str
    amount: Decimal
    remarks: str
    is_within_icici: bool


def _build_fhr_line(total_records: int, txn_date, file_reference: str, total_amount, trace_account_id: str) -> str:
    fields = [
        _field("FHR", "FILEHR", FHR_WIDTHS),
        _field(str(total_records), "TOTAL_NO_OF_RECORDS", FHR_WIDTHS),
        _field(_fmt_date(txn_date), "TRANSACTION_DATE", FHR_WIDTHS),
        _field(file_reference, "EXTERNAL_FILE_REFERENCE", FHR_WIDTHS),
        _field(_fmt_amount(total_amount), "TOTAL_AMOUNT", FHR_WIDTHS),
        _field("INR", "CURRENCY", FHR_WIDTHS),
        _field(trace_account_id, "TRACE_ACCOUNT_ID", FHR_WIDTHS),
        _field(ICICI_CONSTANT_BRANCH_ID, "TRACE_BRANCH_ID", FHR_WIDTHS),
    ]
    return "|".join(fields)


def _build_mdr_line(debit_account_number: str, remitter_name: str, total_amount, remarks: str) -> str:
    fields = [
        _field("MDR", "MDDRT", MDR_WIDTHS),
        _field(debit_account_number, "ACCOUNT_ID_DR", MDR_WIDTHS),
        _field(ICICI_CONSTANT_BRANCH_ID, "BRANCH_ID_DR", MDR_WIDTHS),
        _field(remitter_name, "PAYEE_NAME_DR", MDR_WIDTHS),
        _field(_fmt_amount(total_amount), "AMOUNT_DR", MDR_WIDTHS),
        _field("INR", "CURRENCY_DR", MDR_WIDTHS),
        _field(remarks, "REMARKS_DR", MDR_WIDTHS),
        _field(ICICI_ROUTING_NUMBER, "ROUTING_NUMBER_DR", MDR_WIDTHS),
        _field(NETWORK_ID_WITHIN, "NETWORK_ID_DR", MDR_WIDTHS),
    ]
    return "|".join(fields)


def _build_mco_line(beneficiary: ICICIBeneficiaryRecord) -> str:
    """Multi-Credit, Other bank — NEFT. Beneficiary's own IFSC is the
    routing number."""
    fields = [
        _field("MCO", "MDCROB", MCO_WIDTHS),
        _field(beneficiary.account_number, "ACCOUNT_ID_CRO", MCO_WIDTHS),
        _field(ICICI_CONSTANT_BRANCH_ID, "BRANCH_ID_CRO", MCO_WIDTHS),
        _field(beneficiary.beneficiary_name, "PAYEE_NAME_CRO", MCO_WIDTHS),
        _field(_fmt_amount(beneficiary.amount), "AMOUNT_CRO", MCO_WIDTHS),
        _field("INR", "CURRENCY_CRO", MCO_WIDTHS),
        _field(beneficiary.remarks, "REMARKS_CRO", MCO_WIDTHS),
        _field(NETWORK_ID_OTHER_BANK, "NETWORK_ID_CRO", MCO_WIDTHS),
        _field(beneficiary.ifsc_code, "ROUTING_NUMBER_CRO", MCO_WIDTHS),
    ]
    return "|".join(fields)


def _build_mcw_line(beneficiary: ICICIBeneficiaryRecord) -> str:
    """Multi-Credit, Within ICICI bank. BRANCH_ID_CRW uses the same "0011"
    constant as every other BRANCH_ID_* field — see the module docstring's
    flagged discrepancy with the sheet's "last 4 digits" remark."""
    fields = [
        _field("MCW", "MDCRT", MCW_WIDTHS),
        _field(beneficiary.account_number, "ACCOUNT_ID_CRW", MCW_WIDTHS),
        _field(ICICI_CONSTANT_BRANCH_ID, "BRANCH_ID_CRW", MCW_WIDTHS),
        _field(beneficiary.beneficiary_name, "PAYEE_NAME_CRW", MCW_WIDTHS),
        _field(_fmt_amount(beneficiary.amount), "AMOUNT_CRW", MCW_WIDTHS),
        _field("INR", "CURRENCY_CRW", MCW_WIDTHS),
        _field(beneficiary.remarks, "REMARKS_CRW", MCW_WIDTHS),
        _field(ICICI_ROUTING_NUMBER, "ROUTING_NUMBER_CRW", MCW_WIDTHS),
        _field(NETWORK_ID_WITHIN, "NETWORK_ID_CRW", MCW_WIDTHS),
    ]
    return "|".join(fields)


def build_lines(beneficiaries: list[dict], debit_account: dict, run_metadata: dict) -> list[str]:
    """Builds the raw (unterminated) pipe-delimited lines: FHR, MDR, then
    one MCW/MCO per beneficiary. Raises ICICIBulkFileError if the 500
    total-record limit would be exceeded, or if any non-free-text field
    exceeds its documented max width.
    """
    if len(beneficiaries) > MAX_BENEFICIARIES_PER_FILE:
        raise ICICIBulkFileError(
            f"ICICI's bulk upload limit is {MAX_TOTAL_RECORDS} total records per file (1 remitter/MDR "
            f"record + up to {MAX_BENEFICIARIES_PER_FILE} beneficiary records). This batch has "
            f"{len(beneficiaries)} beneficiaries, which would need {len(beneficiaries) + 1} records. "
            "Split the payroll run into multiple disbursement batches before retrying — multi-file "
            "splitting is not yet automated."
        )

    debit_account_number = debit_account.get("debit_account_number", "")
    remitter_name = debit_account.get("debit_account_company_name") or debit_account.get("debit_account_bank_name", "")
    period_label = run_metadata.get("period_yyyymm", "")
    txn_date = run_metadata.get("pay_date") or date.today().isoformat()
    run_id = run_metadata.get("run_id", "")
    file_reference = f"SAL{run_id}"
    default_remarks = f"SALARY{period_label}"

    records: list[ICICIBeneficiaryRecord] = []
    for beneficiary in beneficiaries:
        amount = Decimal(str(beneficiary["amount"]))
        records.append(
            ICICIBeneficiaryRecord(
                account_number=beneficiary["account_number"],
                ifsc_code=beneficiary["ifsc_code"],
                beneficiary_name=beneficiary["beneficiary_name"],
                amount=amount,
                remarks=beneficiary.get("narration") or default_remarks,
                is_within_icici=is_within_icici(beneficiary["ifsc_code"]),
            )
        )

    total_amount = sum((r.amount for r in records), Decimal("0"))
    total_records = 1 + len(records)  # MDR + one per beneficiary (matches the decoded sample's math)

    lines = [
        _build_fhr_line(total_records, txn_date, file_reference, total_amount, debit_account_number),
        _build_mdr_line(debit_account_number, remitter_name, total_amount, default_remarks),
    ]
    for record in records:
        if record.is_within_icici:
            lines.append(_build_mcw_line(record))
        else:
            lines.append(_build_mco_line(record))
    return lines


def format_bank_file(beneficiaries: list[dict], debit_account: dict, run_metadata: dict) -> bytes:
    """Registry-contract formatter: `(beneficiaries, debit_account,
    run_metadata) -> bytes`, matching generic_neft_xlsx_v1.format_bank_file.
    Returns the raw .txt file bytes (pipe-delimited, "^\\n"-terminated
    lines) — this is both what gets persisted to disk/downloaded for manual
    CIB-portal upload, and (base64-encoded, see
    `format_bank_file_base64`/`encode_base64`) what goes in the Bulk Payment
    API's FILE_CONTENT field.
    """
    lines = build_lines(beneficiaries, debit_account, run_metadata)
    text = "".join(line + LINE_TERMINATOR for line in lines)
    return text.encode("ascii", errors="replace")


def encode_base64(file_bytes: bytes) -> str:
    return base64.b64encode(file_bytes).decode("ascii")


def format_bank_file_base64(beneficiaries: list[dict], debit_account: dict, run_metadata: dict) -> str:
    """Same content as `format_bank_file`, base64-encoded for the Bulk
    Payment API's FILE_CONTENT field (see app/services/icici_api/client.py)."""
    return encode_base64(format_bank_file(beneficiaries, debit_account, run_metadata))
