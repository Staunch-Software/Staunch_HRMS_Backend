"""Tests for the ICICI Composite Pay API client
(app/services/icici_api/client.py). Every HTTP call is mocked via
httpx.MockTransport — NO real network call is ever made to any ICICI
domain (sandbox or live) by this test suite, since no real credentials
exist yet.

Covers: request bodies built correctly for all three calls, exceptions
raised for each documented failure shape, Reverse MIS per-transaction
record parsing, and that calling any of the three methods without the
required config set raises ICICIConfigurationError before any network
call is attempted.
"""
import json

import httpx
import pytest

from app.core.config import Settings
from app.services.icici_api.client import ICICIClient
from app.services.icici_api.exceptions import (
    ICICIBulkPaymentError,
    ICICIConfigurationError,
    ICICIOTPError,
    ICICIStatusCheckError,
)

CONFIGURED_SETTINGS = Settings(
    ICICI_API_KEY="test-api-key",
    ICICI_CORP_ID="PRACHICIB1",
    ICICI_USER_ID="USER3",
    ICICI_AGGR_ID="AGGR0356",
    ICICI_AGGR_NAME="ZAGGLE",
    ICICI_URN="ABCDEFGHIJ",
    ICICI_BASE_URL="https://apibankingonesandbox.icicibank.com",
)

UNCONFIGURED_SETTINGS = Settings(
    ICICI_API_KEY="",
    ICICI_CORP_ID="",
    ICICI_USER_ID="",
    ICICI_AGGR_ID="",
    ICICI_AGGR_NAME="",
    ICICI_URN="",
)

# The exact Reverse MIS success sample from Composite_Pay_API_1.23.pdf page 67.
REVERSE_MIS_SUCCESS_SAMPLE = {
    "XML": {
        "FILE_SEQUENCE_NUM": 24717796,
        "FILE_STATUS": "STS",
        "FILEUPLOAD_BINARY_OUTPUT": {
            "Records": {
                "content": "##### Output File For 7575489478 ####",
                "Record": [
                    "Transaction Type|Network Id|Credit Account Number|Debit Account Number|IFSC Code|Total "
                    "Amount|Host Reference Number|Host Response Code|Host Response Message|Transaction Remarks|"
                    "Transaction Status",
                    "PMT|WIB|0122SLBWYTR0|668505603423|ICIC0000011|INR|4.0|032404654191|000|Payment "
                    "Success|API|SUC",
                    "PMT|WIB|668501413114|0122SLBWYTR0|ICIC0000011|INR|1.0|032404660731|000|Payment "
                    "Success|NEWBULKTEST|SUC",
                    "PMT|NFT|50200022523734|0122SLBWYTR0|HDFC0004256|INR|1.0|032404660761|000|Payment "
                    "Success|OTHERTEST|SUC",
                ],
            }
        },
        "RESPONSE": "SUCCESS",
    }
}


class _RecordingTransport(httpx.BaseTransport):
    """Wraps a response-building function and records every request it
    receives, so tests can assert exact request bodies/headers."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)

    def last_body(self) -> dict:
        return json.loads(self.requests[-1].content.decode("utf-8"))


def _client_with(settings: Settings, handler) -> tuple[ICICIClient, _RecordingTransport]:
    transport = _RecordingTransport(handler)
    client = ICICIClient(settings=settings, transport=transport)
    return client, transport


def _json_response(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _unreachable_handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
    raise AssertionError(f"No network call should have been attempted, got: {request.url}")


# --- Configuration guard -------------------------------------------------


@pytest.mark.parametrize("method_name", ["create_otp", "bulk_payment", "reverse_mis"])
def test_missing_config_raises_before_any_network_call(method_name):
    client, transport = _client_with(UNCONFIGURED_SETTINGS, _unreachable_handler)
    with pytest.raises(ICICIConfigurationError):
        if method_name == "create_otp":
            client.create_otp("UNIQUE1")
        elif method_name == "bulk_payment":
            client.bulk_payment(unique_id="UNIQUE1", otp="123456", file_name="f.txt", file_content_base64="AA==")
        else:
            client.reverse_mis(unique_id="UNIQUE1", file_seq_num="123")
    assert transport.requests == []


# --- Create OTP -----------------------------------------------------------


def test_create_otp_success_request_and_response():
    def handler(request):
        assert request.url.path == "/api/Corporate/CIB/v1/Create"
        assert request.headers["apikey"] == "test-api-key"
        return _json_response({"MESSAGE": "Success", "ResponseCode": "0000", "RESPONSE": "Success"})

    client, transport = _client_with(CONFIGURED_SETTINGS, handler)
    result = client.create_otp("UNIQUE-123")

    assert result["RESPONSE"] == "Success"
    body = transport.last_body()
    assert body == {
        "AGGRID": "AGGR0356",
        "AGGRNAME": "ZAGGLE",
        "CORPID": "PRACHICIB1",
        "USERID": "USER3",
        "URN": "ABCDEFGHIJ",
        "UNIQUEID": "UNIQUE-123",
    }


def test_create_otp_failure_raises_with_error_code():
    def handler(request):
        return _json_response(
            {"Response": "Failure", "Message": "Invalid Corp ID or User ID or Aggregator ID is passed.", "ErrorCode": "9906"}
        )

    client, _ = _client_with(CONFIGURED_SETTINGS, handler)
    with pytest.raises(ICICIOTPError) as exc_info:
        client.create_otp("UNIQUE-123")
    assert "9906" in str(exc_info.value)
    assert exc_info.value.error_code == "9906"


# --- Bulk Payment -----------------------------------------------------------


def test_bulk_payment_rejects_non_txt_filename_without_network_call():
    client, transport = _client_with(CONFIGURED_SETTINGS, _unreachable_handler)
    with pytest.raises(ValueError, match=r"\.txt"):
        client.bulk_payment(unique_id="U1", otp="111111", file_name="bad.xlsx", file_content_base64="AA==")
    assert transport.requests == []


def test_bulk_payment_success_request_body_and_stp_message():
    def handler(request):
        assert request.url.path == "/api/v1/composite-payment"
        assert request.headers["x-priority"] == "BULK"
        return _json_response(
            {
                "CORP_ID": "PRACHICIB1",
                "USER_ID": "USER3",
                "AGGR_NAME": "ZAGGLE",
                "AGGR_ID": "AGGR0356",
                "URN": "ABCDEFGHIJ",
                "UNIQUE_ID": "U1",
                "FILE_SEQUENCE_NUM": "24717796",
                "MESSAGE_DESC": "The file is uploaded successfully. File Sequence No: [24717796]",
            }
        )

    client, transport = _client_with(CONFIGURED_SETTINGS, handler)
    result = client.bulk_payment(
        unique_id="U1", otp="111111", file_name="11111.txt", file_content_base64="RkhSfA==",
        file_description="TEST FILE",
    )

    assert result["FILE_SEQUENCE_NUM"] == "24717796"
    assert result["requires_manual_approval"] is False

    body = transport.last_body()
    assert body == {
        "CORP_ID": "PRACHICIB1",
        "USER_ID": "USER3",
        "AGGR_NAME": "ZAGGLE",
        "AGGR_ID": "AGGR0356",
        "URN": "ABCDEFGHIJ",
        "UNIQUE_ID": "U1",
        "FILE_DESCRIPTION": "TEST FILE",
        "AGOTP": "111111",
        "FILE_NAME": "11111.txt",
        "FILE_CONTENT": "RkhSfA==",
    }


def test_bulk_payment_success_maker_checker_message_flags_manual_approval():
    def handler(request):
        return _json_response(
            {
                "FILE_SEQUENCE_NUM": "24717797",
                "MESSAGE_DESC": "The file upload request is sent for corporate approval successfully. "
                "File Sequence No: [24717797]",
            }
        )

    client, _ = _client_with(CONFIGURED_SETTINGS, handler)
    result = client.bulk_payment(unique_id="U2", otp="222222", file_name="f.txt", file_content_base64="AA==")
    assert result["requires_manual_approval"] is True


def test_bulk_payment_failure_raises_with_error_code():
    def handler(request):
        return _json_response(
            {
                "Message": "Transactions already processed with same unique ID, please use exclusive unique "
                "id for each transaction.",
                "ErrorCode": "999936",
                "Response": "Failure",
            }
        )

    client, _ = _client_with(CONFIGURED_SETTINGS, handler)
    with pytest.raises(ICICIBulkPaymentError) as exc_info:
        client.bulk_payment(unique_id="U3", otp="333333", file_name="f.txt", file_content_base64="AA==")
    assert exc_info.value.error_code == "999936"


# --- Reverse MIS (status check) --------------------------------------------


def test_reverse_mis_success_parses_transactions():
    def handler(request):
        assert request.url.path == "/api/v1/composite-status"
        assert request.headers["x-priority"] == "BULK"
        return _json_response(REVERSE_MIS_SUCCESS_SAMPLE)

    client, transport = _client_with(CONFIGURED_SETTINGS, handler)
    result = client.reverse_mis(unique_id="U1", file_seq_num="24717796")

    assert result["file_sequence_num"] == 24717796
    assert result["file_status"]["code"] == "STS"
    assert result["file_status"]["outcome"] == "success"
    assert len(result["transactions"]) == 3

    first = result["transactions"][0]
    # 12-field layout (with Currency) — see client.py's flagged discrepancy
    # note on the PDF's own sample having one more field than its header.
    assert first["transaction_type"] == "PMT"
    assert first["network_id"] == "WIB"
    assert first["credit_account_number"] == "0122SLBWYTR0"
    assert first["debit_account_number"] == "668505603423"
    assert first["ifsc_code"] == "ICIC0000011"
    assert first["currency"] == "INR"
    assert first["total_amount"] == "4.0"
    assert first["host_reference_number"] == "032404654191"
    assert first["host_response_code"] == "000"
    assert first["host_response_message"] == "Payment Success"
    assert first["transaction_remarks"] == "API"
    assert first["transaction_status"] == "SUC"

    third = result["transactions"][2]
    assert third["network_id"] == "NFT"
    assert third["ifsc_code"] == "HDFC0004256"

    body = transport.last_body()
    assert body == {
        "AGGRID": "AGGR0356",
        "CORPID": "PRACHICIB1",
        "USERID": "USER3",
        "URN": "ABCDEFGHIJ",
        "UNIQUEID": "U1",
        "FILESEQNUM": "24717796",
        "ISENCRYPTED": "Y",
    }


def test_reverse_mis_failure_raises():
    def handler(request):
        return _json_response(
            {"XML": {"RESPONSE": "FAILURE", "Message": "File sequence number not found.", "ErrorCode": "999999"}}
        )

    client, _ = _client_with(CONFIGURED_SETTINGS, handler)
    with pytest.raises(ICICIStatusCheckError):
        client.reverse_mis(unique_id="U1", file_seq_num="00000")


def test_reverse_mis_handles_11_field_legacy_row_shape():
    """Robustness check: if a response ever does match the PDF's literally
    documented 11-field header (no Currency column), parsing still works
    rather than throwing."""
    sample = {
        "XML": {
            "FILE_SEQUENCE_NUM": 1,
            "FILE_STATUS": "FAL",
            "FILEUPLOAD_BINARY_OUTPUT": {
                "Records": {
                    "Record": [
                        "Transaction Type|Network Id|Credit Account Number|Debit Account Number|IFSC Code|"
                        "Total Amount|Host Reference Number|Host Response Code|Host Response Message|"
                        "Transaction Remarks|Transaction Status",
                        "PMT|WIB|ACC1|ACC2|ICIC0000011|4.0|REF1|000|Payment Success|REMARK|SUC",
                    ]
                }
            },
            "RESPONSE": "SUCCESS",
        }
    }

    def handler(request):
        return _json_response(sample)

    client, _ = _client_with(CONFIGURED_SETTINGS, handler)
    result = client.reverse_mis(unique_id="U1", file_seq_num="1")
    assert result["file_status"]["code"] == "FAL"
    assert result["file_status"]["outcome"] == "failure"
    txn = result["transactions"][0]
    assert txn["total_amount"] == "4.0"
    assert "currency" not in txn
