"""Error code / file-status lookup tables, transcribed directly from the
"BULK error codes" tab of
backend/docs/icici/Composite_API_ErrorCodes_1.23.xlsx.

`BULK_TRANSACTION_ERROR_SCENARIOS` keys on the response code ICICI returns
for a failed Create OTP / Bulk Payment call, each mapping to the sheet's
(scenario, message) pair so a caller can show a precise, bank-provided
explanation rather than just the raw code. A response code column
sometimes lists more than one code separated by "|" (e.g. "104668|110370")
— both are registered.

`FILE_STATUS_DESCRIPTIONS` is the Reverse MIS API's FILE_STATUS code table
(sheet rows 19-30). `FILE_STATUS_OUTCOME` classifies each code as
"success" / "failure" / "in_progress" so callers can branch without
hardcoding the bank's internal state names.
"""

BULK_TRANSACTION_ERROR_SCENARIOS: dict[str, dict[str, str]] = {
    "108363": {
        "scenario": "Header date is a past date",
        "message": "The entered date cannot be prior to the current date.",
    },
    "108590": {
        "scenario": "Header amount does not match the sum of record amounts",
        "message": "The header amount does not equal the sum of records in the uploaded file.",
    },
    "101043": {
        "scenario": "Functionality not available",
        "message": "Type system exception occurred",
    },
    "999481": {
        "scenario": "User doesn't have access to upload the file",
        "message": (
            "Dear Customer, This facility is available for select customer segments only. "
            "For any further queries please write to corporatecare@icicibank.com"
        ),
    },
    "108588": {
        "scenario": "Sum of records does not match the total number of records",
        "message": "The total number of records is not same in header and file records.",
    },
    "104668": {
        "scenario": "File is not proper, or has already been uploaded",
        "message": "Please select the proper files and attach again.",
    },
    "110370": {
        "scenario": "File is not proper, or has already been uploaded",
        "message": "Please select the proper files and attach again.",
    },
    "104344": {
        "scenario": "Cut-off time has passed",
        "message": (
            "The cut-off time for this transaction has already passed. This action cannot be "
            "performed with the current transaction date."
        ),
    },
    "999936": {
        "scenario": "Duplicate UNIQUE_ID in the request",
        "message": "Transactions already processed with same unique ID, please use exclusive unique id for each transaction.",
    },
    "111267": {
        "scenario": "Record ID not present in the file",
        "message": "The record ID is not present in the file. Record No: [1].",
    },
    "110004": {
        "scenario": "Entered execution date is a bank holiday",
        "message": "Enter the valid date as the selected date is a bank holiday.",
    },
    "994006": {
        "scenario": "OTP mismatch",
        "message": "OTP Validation Failed",
    },
    "107889": {
        "scenario": "OTP mismatch",
        "message": "OTP Validation Failed",
    },
    "100901": {
        "scenario": "Consumption limits not defined for the user",
        "message": "Consumption limits not defined for the user. Transaction cannot be processed. Please contact the bank administrator",
    },
    "104666": {
        "scenario": "Duplicate file name",
        "message": "File with the same name is already uploaded",
    },
    # Generic/CIB-level failure also documented in the PDF samples (Create
    # OTP / Beneficiary Registration failure response), not listed with a
    # dedicated row in the sheet's scenario table.
    "9906": {
        "scenario": "Invalid identifiers",
        "message": "Invalid Corp ID or User ID or Aggregator ID is passed.",
    },
}

# Reverse MIS API FILE_STATUS codes (sheet rows 19-30).
FILE_STATUS_DESCRIPTIONS: dict[str, str] = {
    "GIP": "Intermediate state while GFP batches get executed.",
    "PFI": (
        "Pending for insertion — bulk has been uploaded and the transaction is completed from the "
        "front end; the batch process is queued to complete."
    ),
    "ENT": "Entered state for the transaction once the bulk transaction is initiated.",
    "MIR": "Manual intervention required — goes for reversal.",
    "STS": "Success",
    "FAL": "Failure",
    "PPD": "Partially processed",
    "REJ": "Transaction has gone to rejected case.",
    "ATH": "Status after the process-scheduler batch run is completed (before the GFP batch).",
    "CRP": "Credit reversal pending",
    "REC": "Initiator itself cancelled or recalled the transaction.",
}

# Human-readable classification used by the status-check endpoint to decide
# what to tell the caller next (poll again later vs. done).
FILE_STATUS_OUTCOME: dict[str, str] = {
    "STS": "success",
    "FAL": "failure",
    "REJ": "failure",
    "PPD": "partial",
    "GIP": "in_progress",
    "PFI": "in_progress",
    "ENT": "in_progress",
    "ATH": "in_progress",
    "MIR": "needs_attention",
    "CRP": "needs_attention",
    "REC": "cancelled",
}


def describe_file_status(file_status: str) -> dict:
    code = (file_status or "").strip().upper()
    return {
        "code": code,
        "description": FILE_STATUS_DESCRIPTIONS.get(code, f"Unrecognized FILE_STATUS code '{code}'."),
        "outcome": FILE_STATUS_OUTCOME.get(code, "unknown"),
    }


def lookup_error(error_code: str) -> dict:
    code = str(error_code) if error_code is not None else ""
    entry = BULK_TRANSACTION_ERROR_SCENARIOS.get(code)
    if entry:
        return {"error_code": code, **entry}
    return {"error_code": code, "scenario": "Unrecognized error code", "message": None}
