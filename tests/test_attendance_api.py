from datetime import date

import pytest

from app.models.employee import Employee
from app.models.org import Department
from app.services.attendance import sync as sync_module
from tests.attendance_fakes import FakeMatrix, rec

D28 = date(2026, 10, 28)
D29 = date(2026, 10, 29)


@pytest.fixture()
def fake_matrix(monkeypatch):
    fake = FakeMatrix({
        D29: [
            rec("STINT0007", "28/10/2026 18:33:44", "0", "New Office", received="10/29/2026 09:10:29"),
            rec("STINT0007", "28/10/2026 09:00:00", "0", "New Office", received="10/29/2026 09:10:29"),
            rec("STEMP0192", "29/10/2026 09:00:00", "0", username="Kaviarasan B"),
            rec("STEMP0192", "29/10/2026 18:00:00", "1", username="Kaviarasan B"),
            rec("STEMP0050", "29/10/2026 10:00:00", "0", "New Office"),
        ]
    })
    monkeypatch.setattr(sync_module, "MatrixClient", lambda: fake.client())
    return fake


def _sync(client, date_from="2026-10-29", date_to="2026-10-29"):
    resp = client.post("/api/v1/attendance/sync", json={"date_from": date_from, "date_to": date_to})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_requires_auth(client):
    resp = client.get("/api/v1/attendance/daily", headers={"Authorization": ""})
    assert resp.status_code == 401


def test_sync_endpoint_returns_run(client, fake_matrix):
    run = _sync(client)
    assert run["status"] == "success"
    assert run["triggered_by"] == "manual"
    assert run["records_fetched"] == 5 and run["records_new"] == 5 and run["days_recomputed"] == 3

    runs = client.get("/api/v1/attendance/sync-runs?limit=5").json()
    assert len(runs) == 1 and runs[0]["id"] == run["id"]


def test_sync_rejects_long_or_inverted_range(client, fake_matrix):
    assert client.post("/api/v1/attendance/sync", json={"date_from": "2026-09-01", "date_to": "2026-10-15"}).status_code == 400
    assert client.post("/api/v1/attendance/sync", json={"date_from": "2026-10-29", "date_to": "2026-10-28"}).status_code == 400
    assert not fake_matrix.requests


def test_sync_failure_is_reported_on_run(client, fake_matrix):
    fake_matrix.fail_with = 500
    run = _sync(client)
    assert run["status"] == "failed" and "HTTP 500" in run["error_message"]


def test_daily_and_punches_endpoints(client, db_session, fake_matrix):
    _sync(client)
    rows = client.get("/api/v1/attendance/daily?date_from=2026-10-28&date_to=2026-10-29").json()
    by_key = {(r["employee_code"], r["work_date"]): r for r in rows}
    assert set(by_key) == {("STINT0007", "2026-10-28"), ("STEMP0192", "2026-10-29"), ("STEMP0050", "2026-10-29")}

    late = by_key[("STINT0007", "2026-10-28")]
    assert late["is_fallback_out"] is True and late["punch_out_source"] == "FALLBACK_LAST_PUNCH"
    assert late["is_matched"] is False and late["employee_id"] is None

    single = by_key[("STEMP0050", "2026-10-29")]
    assert single["is_single_punch"] is True and single["punch_out"] is None

    full = by_key[("STEMP0192", "2026-10-29")]
    assert full["worked_minutes"] == 540 and full["username"] == "Kaviarasan B"

    only = client.get("/api/v1/attendance/daily?date_from=2026-10-29&employee_code=ST-EMP-192").json()
    assert [r["employee_code"] for r in only] == ["STEMP0192"]

    punches = client.get("/api/v1/attendance/punches?work_date=2026-10-28&employee_code=STINT0007").json()
    assert [p["event_datetime"] for p in punches] == ["2026-10-28T09:00:00", "2026-10-28T18:33:44"]
    assert punches[0]["direction"] == "IN" and punches[0]["received_at"] == "2026-10-29T09:10:29"


def test_new_employee_shows_as_matched_without_resync(client, db_session, fake_matrix):
    _sync(client)
    requests_before = len(fake_matrix.requests)
    dept = Department(name="Engineering", code="ENG")
    db_session.add(dept)
    db_session.flush()
    db_session.add(Employee(employee_code="ST-EMP-0192", first_name="Kaviarasan", last_name="B", department_id=dept.id))
    db_session.commit()

    rows = client.get("/api/v1/attendance/daily?date_from=2026-10-29&employee_code=STEMP0192").json()
    assert rows[0]["is_matched"] is True
    assert rows[0]["employee_name"] == "Kaviarasan B"
    assert rows[0]["department"] == "Engineering"
    assert len(fake_matrix.requests) == requests_before

    assert client.post("/api/v1/attendance/relink").json() == {"codes_relinked": 1}


def test_monthly_summary(client, fake_matrix):
    _sync(client)
    summary = client.get("/api/v1/attendance/monthly-summary?month=2026-10").json()
    assert summary["date_from"] == "2026-10-01" and summary["date_to"] == "2026-10-31"
    by_code = {e["employee_code"]: e for e in summary["employees"]}
    assert by_code["STEMP0192"]["days_present"] == 1
    assert by_code["STEMP0192"]["total_worked_minutes"] == 540
    assert by_code["STINT0007"]["days_with_fallback_out"] == 1
    assert by_code["STEMP0050"]["total_worked_minutes"] == 0

    assert client.get("/api/v1/attendance/monthly-summary?month=2026-13").status_code == 400
