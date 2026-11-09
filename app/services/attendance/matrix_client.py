"""Client for the Matrix COSEC (vyom) `event-ta-date` API.

The date-range filter applies to `idatetime` (when the Matrix server received
the punch), not to the punch time itself, so callers must group by the parsed
`eventdatetime` and sync a rolling window. Two different datetime formats:
  eventdatetime: DD/MM/YYYY HH:MM:SS (punch time, local IST)
  idatetime:     MM/DD/YYYY HH:MM:SS (server receive time)
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import httpx

from app.core.config import settings
from app.services.employee_code import normalize_employee_code

EVENT_DATETIME_FORMAT = "%d/%m/%Y %H:%M:%S"
RECEIVED_DATETIME_FORMAT = "%m/%d/%Y %H:%M:%S"
RESPONSE_KEY = "event-ta-date"


class MatrixAPIError(Exception):
    pass


@dataclass(frozen=True)
class MatrixPunch:
    employee_code: str
    raw_userid: str
    username: Optional[str]
    event_datetime: datetime
    entry_exit_type: str
    device_name: str
    received_at: Optional[datetime]

    @property
    def event_date(self) -> date:
        return self.event_datetime.date()


def build_url(base_url: str, day: date) -> str:
    stamp = day.strftime("%d%m%Y")
    return f"{base_url}?action=get;date-range={stamp}000000-{stamp}235959;format=json"


def parse_event_datetime(value: str) -> datetime:
    return datetime.strptime(value.strip(), EVENT_DATETIME_FORMAT)


def parse_received_at(value: Optional[str]) -> Optional[datetime]:
    if not value or not str(value).strip():
        return None
    return datetime.strptime(str(value).strip(), RECEIVED_DATETIME_FORMAT)


def parse_record(record: dict) -> MatrixPunch:
    raw_userid = str(record.get("userid") or "").strip()
    if not raw_userid:
        raise ValueError("record has no userid")
    entry_exit_type = str(record.get("entryexittype") or "").strip()
    if entry_exit_type not in ("0", "1"):
        raise ValueError(f"unknown entryexittype {entry_exit_type!r}")
    return MatrixPunch(
        employee_code=normalize_employee_code(raw_userid),
        raw_userid=raw_userid,
        username=(str(record.get("username")).strip() or None) if record.get("username") is not None else None,
        event_datetime=parse_event_datetime(str(record.get("eventdatetime") or "")),
        entry_exit_type=entry_exit_type,
        device_name=str(record.get("device_name") or "").strip(),
        received_at=parse_received_at(record.get("idatetime")),
    )


class MatrixClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        timeout: Optional[float] = None,
        transport: Optional[httpx.BaseTransport] = None,
    ):
        self.base_url = base_url if base_url is not None else settings.MATRIX_API_BASE_URL
        self.username = username if username is not None else settings.MATRIX_API_USERNAME
        self.password = password if password is not None else settings.MATRIX_API_PASSWORD
        self.timeout = timeout if timeout is not None else settings.MATRIX_API_TIMEOUT_SECONDS
        self.transport = transport

    def fetch_day_raw(self, day: date) -> list[dict]:
        """Records the Matrix server RECEIVED on `day` (raw dicts)."""
        if not self.base_url or not self.username or not self.password:
            raise MatrixAPIError(
                "Matrix API is not configured: set MATRIX_API_BASE_URL, MATRIX_API_USERNAME "
                "and MATRIX_API_PASSWORD in backend/.env."
            )
        url = build_url(self.base_url, day)
        try:
            with httpx.Client(
                auth=(self.username, self.password), timeout=self.timeout, transport=self.transport
            ) as client:
                response = client.get(url)
        except httpx.HTTPError as exc:
            raise MatrixAPIError(f"Matrix API request failed for {day:%d-%m-%Y}: {exc.__class__.__name__}: {exc}") from exc

        if response.status_code in (401, 403):
            raise MatrixAPIError(
                f"Matrix API rejected the credentials (HTTP {response.status_code}). "
                "Check MATRIX_API_USERNAME / MATRIX_API_PASSWORD."
            )
        if response.status_code != 200:
            raise MatrixAPIError(
                f"Matrix API returned HTTP {response.status_code} for {day:%d-%m-%Y}: {response.text[:200]}"
            )

        text = response.text.strip()
        # A day with no data comes back as text/plain, e.g.
        # "success: 0040100000 : No records found".
        if not text or (text.lower().startswith("success") and "no records" in text.lower()):
            return []
        try:
            payload = response.json()
        except ValueError as exc:
            raise MatrixAPIError(f"Matrix API returned non-JSON for {day:%d-%m-%Y}: {text[:200]}") from exc
        if not isinstance(payload, dict):
            raise MatrixAPIError(f"Matrix API returned unexpected JSON for {day:%d-%m-%Y}: {text[:200]}")
        records = payload.get(RESPONSE_KEY)
        if records is None:
            return []
        if not isinstance(records, list):
            raise MatrixAPIError(f"Matrix API '{RESPONSE_KEY}' is not a list for {day:%d-%m-%Y}")
        return records

    def fetch_day(self, day: date) -> list[MatrixPunch]:
        punches = []
        for record in self.fetch_day_raw(day):
            try:
                punches.append(parse_record(record))
            except (ValueError, TypeError) as exc:
                raise MatrixAPIError(f"Unparseable Matrix record {record!r}: {exc}") from exc
        return punches
