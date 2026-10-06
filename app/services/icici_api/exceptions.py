"""Exception hierarchy for the ICICI Composite Pay API client.

Every failure response from ICICI (or a missing-config situation caught
before a network call is even attempted) raises one of these, carrying the
bank's own error code/message where available, so a caller gets a clear,
actionable error instead of a generic HTTP failure.
"""
from typing import Optional


class ICICIAPIError(Exception):
    """Base class. `error_code`/`message` are ICICI's own values (from the
    `BULK error codes` sheet's "Bulk transaction API" table, or echoed
    directly from a Create OTP / Bulk Payment / Reverse MIS failure
    response) when available."""

    def __init__(self, message: str, error_code: Optional[str] = None, response_payload: Optional[dict] = None):
        self.error_code = error_code
        self.response_payload = response_payload
        full_message = f"[{error_code}] {message}" if error_code else message
        super().__init__(full_message)


class ICICIConfigurationError(ICICIAPIError):
    """Raised before any network call when required settings
    (ICICI_CORP_ID/ICICI_USER_ID/ICICI_AGGR_ID/ICICI_AGGR_NAME/ICICI_URN/
    ICICI_API_KEY, or the RSA key material if encryption is enabled) are
    missing. Never caused by a real ICICI response."""


class ICICIOTPError(ICICIAPIError):
    """Create OTP API returned a failure response."""


class ICICIBulkPaymentError(ICICIAPIError):
    """Bulk Payment API returned a FILE REJECTED response."""


class ICICIStatusCheckError(ICICIAPIError):
    """Reverse MIS (status check) API returned a non-success response."""
