"""Fake Matrix COSEC API for attendance tests (never hits the real API)."""
import json
import re
from datetime import date, datetime

import httpx

from app.services.attendance.matrix_client import MatrixClient

_RANGE_RE = re.compile(r"date-range=(\d{8})000000-(\d{8})235959")


def rec(userid, event, kind, device="Main Door", received=None, username=None):
    """event: 'DD/MM/YYYY HH:MM:SS'; received defaults to event in MM/DD/YYYY form."""
    if received is None:
        received = datetime.strptime(event, "%d/%m/%Y %H:%M:%S").strftime("%m/%d/%Y %H:%M:%S")
    return {
        "userid": userid,
        "username": username or f"User {userid}",
        "eventdatetime": event,
        "entryexittype": kind,
        "device_name": device,
        "idatetime": received,
    }


class FakeMatrix:
    """Serves records keyed by the RECEIVE date (as the real API filters)."""

    def __init__(self, by_day: dict[date, list[dict]] | None = None):
        self.by_day = by_day or {}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None
        self.raw_body: str | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, text="error")
        if self.raw_body is not None:
            return httpx.Response(200, text=self.raw_body)
        match = _RANGE_RE.search(str(request.url))
        assert match, f"unexpected URL {request.url}"
        day = datetime.strptime(match.group(1), "%d%m%Y").date()
        records = self.by_day.get(day, [])
        if not records:
            return httpx.Response(200, text="success: 0040100000 : No records found",
                                  headers={"content-type": "text/plain;charset=utf-8"})
        return httpx.Response(200, text=json.dumps({"event-ta-date": records}),
                              headers={"content-type": "application/json;charset=utf-8"})

    def client(self) -> MatrixClient:
        return MatrixClient(
            base_url="https://matrix.test/api.svc/v2/event-ta-date",
            username="user",
            password="secret",
            transport=httpx.MockTransport(self.handler),
        )
