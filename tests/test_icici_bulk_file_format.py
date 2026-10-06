"""Tests for the REAL ICICI bulk salary file format
(app/services/bank_file/icici_bulk_salary_txt_v1.py), transcribed from
ICICI's Composite Pay API docs and cross-checked against the real
base64-encoded sample FILE_CONTENT in Composite_Pay_API_1.23.pdf page 61.

This is the most important test in the whole ICICI integration: a
one-character-off field width or a wrong field order would corrupt a real
bank file used to pay real salaries, so every field position for every
record type (FHR/MDR/MCW/MCO) is asserted exactly, not just "roughly
right."
"""
import base64
from decimal import Decimal

import pytest

from app.services.bank_file.icici_bulk_salary_txt_v1 import (
    MAX_BENEFICIARIES_PER_FILE,
    ICICIBulkFileError,
    build_lines,
    format_bank_file,
    format_bank_file_base64,
)

DEBIT_ACCOUNT = {
    "debit_account_number": "000705000961",
    "debit_account_bank_name": "ICICI Bank",
    "debit_account_company_name": "STAUNCH",
}
RUN_METADATA = {
    "period_yyyymm": "202610",
    "pay_date": "2026-10-05",
    "run_id": 42,
}

ICICI_BENEFICIARY = {
    "beneficiary_name": "SOURABH",
    "account_number": "000405011155",
    "ifsc_code": "ICIC0005678",
    "amount": Decimal("1.00"),
    "narration": "TestJUN",
}
OTHER_BANK_BENEFICIARY = {
    "beneficiary_name": "RAKESH",
    "account_number": "000451000301",
    "ifsc_code": "DLXB0000092",
    "amount": Decimal("1.00"),
    "narration": "OTHERTEST",
}


def test_mixed_batch_exact_field_layout():
    beneficiaries = [ICICI_BENEFICIARY, OTHER_BANK_BENEFICIARY]
    lines = build_lines(beneficiaries, DEBIT_ACCOUNT, RUN_METADATA)
    assert len(lines) == 4  # FHR + MDR + MCW + MCO

    fhr = lines[0].split("|")
    assert fhr == [
        "FHR",
        "3",  # 1 MDR + 2 beneficiary records
        "10/05/2026",  # MM/DD/YYYY
        "SAL42",
        "2.00",  # sum of both beneficiary amounts
        "INR",
        "000705000961",
        "0011",
    ]

    mdr = lines[1].split("|")
    assert mdr == [
        "MDR",
        "000705000961",
        "0011",
        "STAUNCH",
        "2.00",
        "INR",
        "SALARY202610",
        "ICIC0000011",
        "WIB",
    ]

    mcw = lines[2].split("|")
    assert mcw == [
        "MCW",
        "000405011155",
        "0011",  # constant, NOT "last 4 digits" (see module docstring)
        "SOURABH",
        "1.00",
        "INR",
        "TestJUN",
        "ICIC0000011",
        "WIB",
    ]

    mco = lines[3].split("|")
    assert mco == [
        "MCO",
        "000451000301",
        "0011",
        "RAKESH",
        "1.00",
        "INR",
        "OTHERTEST",
        "NFT",
        "DLXB0000092",  # beneficiary's own IFSC as routing number
    ]


def test_line_terminator_is_caret_newline():
    file_bytes = format_bank_file([ICICI_BENEFICIARY], DEBIT_ACCOUNT, RUN_METADATA)
    text = file_bytes.decode("ascii")
    lines = text.split("\n")
    assert lines[-1] == ""  # trailing newline
    for line in lines[:-1]:
        assert line.endswith("^")


def test_only_icici_ifsc_gets_mcw_record():
    lines = build_lines([ICICI_BENEFICIARY], DEBIT_ACCOUNT, RUN_METADATA)
    assert lines[2].startswith("MCW|")

    lines = build_lines([OTHER_BANK_BENEFICIARY], DEBIT_ACCOUNT, RUN_METADATA)
    assert lines[2].startswith("MCO|")


def test_base64_round_trip_matches_raw_bytes():
    beneficiaries = [ICICI_BENEFICIARY, OTHER_BANK_BENEFICIARY]
    raw = format_bank_file(beneficiaries, DEBIT_ACCOUNT, RUN_METADATA)
    encoded = format_bank_file_base64(beneficiaries, DEBIT_ACCOUNT, RUN_METADATA)
    assert base64.b64decode(encoded) == raw


def test_amount_rounds_to_two_decimals():
    beneficiary = {**ICICI_BENEFICIARY, "amount": Decimal("84950.505")}
    lines = build_lines([beneficiary], DEBIT_ACCOUNT, RUN_METADATA)
    mcw_amount = lines[2].split("|")[4]
    assert mcw_amount == "84950.51"  # ROUND_HALF_UP from .505


def test_payee_name_over_32_chars_is_truncated_not_rejected():
    beneficiary = {**ICICI_BENEFICIARY, "beneficiary_name": "A" * 40}
    lines = build_lines([beneficiary], DEBIT_ACCOUNT, RUN_METADATA)
    payee_name = lines[2].split("|")[3]
    assert payee_name == "A" * 32


def test_oversized_account_number_raises_instead_of_truncating():
    beneficiary = {**ICICI_BENEFICIARY, "account_number": "1" * 40}  # > width 34 for MCO path
    beneficiary["ifsc_code"] = OTHER_BANK_BENEFICIARY["ifsc_code"]
    with pytest.raises(ICICIBulkFileError):
        build_lines([beneficiary], DEBIT_ACCOUNT, RUN_METADATA)


def test_exactly_at_500_record_limit_succeeds():
    beneficiaries = [
        {**OTHER_BANK_BENEFICIARY, "account_number": f"{i:012d}"} for i in range(MAX_BENEFICIARIES_PER_FILE)
    ]
    lines = build_lines(beneficiaries, DEBIT_ACCOUNT, RUN_METADATA)
    # FHR + MDR + one per beneficiary = 500 total "records" worth of lines
    # (FHR itself isn't counted in TOTAL_NO_OF_RECORDS, but is a line).
    assert len(lines) == 2 + MAX_BENEFICIARIES_PER_FILE
    fhr_total_records = int(lines[0].split("|")[1])
    assert fhr_total_records == 1 + MAX_BENEFICIARIES_PER_FILE == 500


def test_over_500_record_limit_raises_clear_error():
    beneficiaries = [
        {**OTHER_BANK_BENEFICIARY, "account_number": f"{i:012d}"} for i in range(MAX_BENEFICIARIES_PER_FILE + 1)
    ]
    with pytest.raises(ICICIBulkFileError, match="500"):
        build_lines(beneficiaries, DEBIT_ACCOUNT, RUN_METADATA)
