"""ICICI Bank Composite Pay API client — bulk salary disbursement.

NOT USABLE FOR REAL TRANSACTIONS yet: CORP_ID, USER_ID, AGGR_ID, AGGR_NAME,
and URN are all placeholder-empty until the user's ICICI relationship
manager provides them during onboarding. Every call in `client.py` checks
for these before attempting any network request and raises
`ICICIConfigurationError` if anything is missing — see that module's
docstring.
"""
from app.services.icici_api.client import ICICIClient
from app.services.icici_api.exceptions import (
    ICICIAPIError,
    ICICIConfigurationError,
    ICICIOTPError,
    ICICIBulkPaymentError,
    ICICIStatusCheckError,
)

__all__ = [
    "ICICIClient",
    "ICICIAPIError",
    "ICICIConfigurationError",
    "ICICIOTPError",
    "ICICIBulkPaymentError",
    "ICICIStatusCheckError",
]
