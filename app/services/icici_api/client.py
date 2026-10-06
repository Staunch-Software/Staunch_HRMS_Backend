"""ICICI Composite Pay API client — Create OTP, Bulk Payment, Reverse MIS
(status check). Transcribed from Composite_Pay_API_1.23.pdf pages 59-67.

NOT USABLE FOR REAL TRANSACTIONS YET. Every public method calls
`_check_config()` first and raises `ICICIConfigurationError` — never
attempting a network call — if any of ICICI_API_KEY / ICICI_CORP_ID /
ICICI_USER_ID / ICICI_AGGR_ID / ICICI_AGGR_NAME / ICICI_URN is unset. These
all come from the user's ICICI relationship manager during onboarding and
are not available yet (see app/core/config.py).

Endpoints used (UAT sandbox, the config default — see settings.ICICI_BASE_URL):
    POST {base}/api/Corporate/CIB/v1/Create   — Create OTP
    POST {base}/api/v1/composite-payment      — Bulk Payment
    POST {base}/api/v1/composite-status       — Reverse MIS (status check)

Encryption: when `settings.ICICI_ENCRYPTION_ENABLED` is True, every request
body is wrapped (and every response unwrapped) through
app/services/icici_api/encryption.py's hybrid RSA+AES envelope. Default is
False — plain JSON bodies, matching the examples in the PDF, which is a
common sandbox convenience — but this needs the RM to confirm whether UAT
actually enforces the encrypted envelope.
"""
from typing import Optional

import httpx

from app.core.config import Settings, settings as default_settings
from app.services.icici_api import encryption, error_codes
from app.services.icici_api.exceptions import (
    ICICIBulkPaymentError,
    ICICIConfigurationError,
    ICICIOTPError,
    ICICIStatusCheckError,
)

CREATE_OTP_PATH = "/api/Corporate/CIB/v1/Create"
BULK_PAYMENT_PATH = "/api/v1/composite-payment"
REVERSE_MIS_PATH = "/api/v1/composite-status"

# The 11-name header the PDF's sample Reverse MIS response literally
# documents for each pipe-delimited "Record" row.
_TRANSACTION_FIELDS_11 = [
    "transaction_type", "network_id", "credit_account_number", "debit_account_number",
    "ifsc_code", "total_amount", "host_reference_number", "host_response_code",
    "host_response_message", "transaction_remarks", "transaction_status",
]
# FLAGGED DISCREPANCY: the PDF's own worked sample response
# (Composite_Pay_API_1.23.pdf, page 67) has 12 pipe-delimited values per
# data row, not 11 — there is a "Currency" value ("INR") between IFSC Code
# and Total Amount that the documented header string omits. This module
# parses by the real 12-field layout the sample data actually uses,
# falling back to the documented 11-field layout if a row doesn't have 12
# parts. Confirm with the RM which is authoritative for your account.
_TRANSACTION_FIELDS_12 = [
    "transaction_type", "network_id", "credit_account_number", "debit_account_number",
    "ifsc_code", "currency", "total_amount", "host_reference_number", "host_response_code",
    "host_response_message", "transaction_remarks", "transaction_status",
]

REQUIRED_CORE_SETTINGS = (
    "ICICI_API_KEY",
    "ICICI_CORP_ID",
    "ICICI_USER_ID",
    "ICICI_AGGR_ID",
    "ICICI_AGGR_NAME",
    "ICICI_URN",
)


def missing_config_fields(settings: Settings) -> list[str]:
    """Used by both the client (to refuse a call) and the
    GET /icici/config-status endpoint (to tell the frontend which fields to
    show as missing)."""
    return [name for name in REQUIRED_CORE_SETTINGS if not getattr(settings, name, "")]


class ICICIClient:
    def __init__(self, settings: Optional[Settings] = None, transport: Optional[httpx.BaseTransport] = None):
        self._settings = settings or default_settings
        self._http = httpx.Client(base_url=self._settings.ICICI_BASE_URL, timeout=30.0, transport=transport)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "ICICIClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- config guard --------------------------------------------------

    def _check_config(self) -> None:
        missing = missing_config_fields(self._settings)
        if missing:
            raise ICICIConfigurationError(
                "ICICI API credentials not configured — missing: " + ", ".join(missing) + ". "
                "Obtain these from your ICICI relationship manager and set them in backend/.env."
            )
        if self._settings.ICICI_ENCRYPTION_ENABLED and not self._settings.ICICI_RSA_PUBLIC_KEY_PEM:
            raise ICICIConfigurationError(
                "ICICI_ENCRYPTION_ENABLED is True but ICICI_RSA_PUBLIC_KEY_PEM is not set."
            )

    # --- encryption plumbing --------------------------------------------

    def _wrap_request(self, body: dict, service: str) -> dict:
        if not self._settings.ICICI_ENCRYPTION_ENABLED:
            return body
        return encryption.build_encrypted_request(
            body,
            self._settings.ICICI_RSA_PUBLIC_KEY_PEM.encode("utf-8"),
            service,
        )

    def _unwrap_response(self, data: dict) -> dict:
        if not self._settings.ICICI_ENCRYPTION_ENABLED:
            return data
        if "encryptedData" not in data:
            # Some failure responses are documented as plain JSON even when
            # the success path is encrypted — don't force-decrypt those.
            return data
        if not self._settings.ICICI_RSA_PRIVATE_KEY_PEM:
            raise ICICIConfigurationError(
                "ICICI_ENCRYPTION_ENABLED is True but ICICI_RSA_PRIVATE_KEY_PEM is not set — cannot "
                "decrypt the response."
            )
        return encryption.decrypt_response(data, self._settings.ICICI_RSA_PRIVATE_KEY_PEM.encode("utf-8"))

    def _headers(self, x_priority: Optional[str] = None) -> dict:
        headers = {"apikey": self._settings.ICICI_API_KEY}
        if x_priority:
            headers["x-priority"] = x_priority
        return headers

    # --- 1. Create OTP ---------------------------------------------------

    def create_otp(self, unique_id: str) -> dict:
        self._check_config()
        body = {
            "AGGRID": self._settings.ICICI_AGGR_ID,
            "AGGRNAME": self._settings.ICICI_AGGR_NAME,
            "CORPID": self._settings.ICICI_CORP_ID,
            "USERID": self._settings.ICICI_USER_ID,
            "URN": self._settings.ICICI_URN,
            "UNIQUEID": unique_id,
        }
        response = self._http.post(CREATE_OTP_PATH, json=self._wrap_request(body, "CreateOTP"), headers=self._headers())
        data = self._unwrap_response(response.json())

        response_flag = data.get("RESPONSE") or data.get("Response")
        if response_flag != "Success":
            error_code = data.get("ErrorCode")
            lookup = error_codes.lookup_error(error_code) if error_code else {}
            message = data.get("Message") or lookup.get("message") or "Create OTP API returned a failure response."
            raise ICICIOTPError(message, error_code=error_code, response_payload=data)
        return data

    # --- 2. Bulk Payment --------------------------------------------------

    def bulk_payment(
        self,
        *,
        unique_id: str,
        otp: str,
        file_name: str,
        file_content_base64: str,
        file_description: str = "Staunch salary disbursement",
    ) -> dict:
        self._check_config()
        if not file_name.endswith(".txt"):
            raise ValueError("FILE_NAME must end in .txt — ICICI's Bulk Payment API requires it (see the PDF).")

        body = {
            "CORP_ID": self._settings.ICICI_CORP_ID,
            "USER_ID": self._settings.ICICI_USER_ID,
            "AGGR_NAME": self._settings.ICICI_AGGR_NAME,
            "AGGR_ID": self._settings.ICICI_AGGR_ID,
            "URN": self._settings.ICICI_URN,
            "UNIQUE_ID": unique_id,
            "FILE_DESCRIPTION": file_description,
            "AGOTP": otp,
            "FILE_NAME": file_name,
            "FILE_CONTENT": file_content_base64,
        }
        response = self._http.post(
            BULK_PAYMENT_PATH,
            json=self._wrap_request(body, "BulkPayment"),
            headers=self._headers(x_priority="BULK"),
        )
        data = self._unwrap_response(response.json())

        if data.get("Response") == "Failure":
            error_code = data.get("ErrorCode")
            lookup = error_codes.lookup_error(error_code) if error_code else {}
            message = data.get("Message") or lookup.get("message") or "Bulk Payment API rejected the file (FILE REJECTED)."
            raise ICICIBulkPaymentError(message, error_code=error_code, response_payload=data)

        message_desc = data.get("MESSAGE_DESC", "") or ""
        # Distinguish maker-checker (still needs manual CIB approval) from
        # STP (nothing further needed) — both are "success" responses, only
        # the wording of MESSAGE_DESC differs (PDF page 63).
        requires_manual_approval = "corporate approval" in message_desc.lower()
        return {**data, "requires_manual_approval": requires_manual_approval}

    # --- 3. Reverse MIS (status check) -----------------------------------

    def reverse_mis(self, *, unique_id: str, file_seq_num: str) -> dict:
        self._check_config()
        body = {
            "AGGRID": self._settings.ICICI_AGGR_ID,
            "CORPID": self._settings.ICICI_CORP_ID,
            "USERID": self._settings.ICICI_USER_ID,
            "URN": self._settings.ICICI_URN,
            "UNIQUEID": unique_id,
            "FILESEQNUM": file_seq_num,
            "ISENCRYPTED": "Y",
        }
        response = self._http.post(
            REVERSE_MIS_PATH,
            json=self._wrap_request(body, "ReverseMIS"),
            headers=self._headers(x_priority="BULK"),
        )
        data = self._unwrap_response(response.json())
        # The documented sample response nests everything under "XML".
        inner = data.get("XML", data)

        response_flag = (inner.get("RESPONSE") or "").upper()
        if response_flag not in ("SUCCESS", "") and (inner.get("ErrorCode") or inner.get("Message")):
            error_code = inner.get("ErrorCode")
            lookup = error_codes.lookup_error(error_code) if error_code else {}
            message = inner.get("Message") or lookup.get("message") or "Reverse MIS API returned a failure response."
            raise ICICIStatusCheckError(message, error_code=error_code, response_payload=data)

        return self._parse_reverse_mis(inner)

    @staticmethod
    def _parse_transaction_row(row: str) -> dict:
        parts = row.split("|")
        if len(parts) == len(_TRANSACTION_FIELDS_12):
            return dict(zip(_TRANSACTION_FIELDS_12, parts))
        if len(parts) == len(_TRANSACTION_FIELDS_11):
            return dict(zip(_TRANSACTION_FIELDS_11, parts))
        return {"raw": row, "parse_error": f"Expected 11 or 12 pipe-delimited fields, got {len(parts)}."}

    def _parse_reverse_mis(self, inner: dict) -> dict:
        file_status = inner.get("FILE_STATUS")
        records = (
            (inner.get("FILEUPLOAD_BINARY_OUTPUT") or {}).get("Records", {}).get("Record", [])
        )
        rows = list(records)
        # Record[0] is the column-header line itself (per the sample),
        # not a transaction — drop it if present.
        if rows and rows[0].split("|")[0].strip().lower() == "transaction type":
            rows = rows[1:]
        transactions = [self._parse_transaction_row(row) for row in rows]

        return {
            "file_sequence_num": inner.get("FILE_SEQUENCE_NUM"),
            "file_status": error_codes.describe_file_status(file_status),
            "response": inner.get("RESPONSE"),
            "transactions": transactions,
        }
