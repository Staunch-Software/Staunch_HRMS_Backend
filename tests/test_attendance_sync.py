from datetime import date, datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.models.attendance import AttendanceDaily, AttendancePunch, AttendanceSyncRun
from app.models.employee import Employee
from app.services.attendance.matrix_client import (
    MatrixAPIError,
    build_url,
    parse_event_datetime,
    parse_received_at,
    parse_record,
)
from app.services.attendance.scheduler import parse_sync_time
from app.services.attendance.sync import compute_daily, relink_employees, sync_attendance
from tests.attendance_fakes import FakeMatrix, rec

D28 = date(2026, 10, 28)
D29 = date(2026, 10, 29)
D30 = date(2026, 10, 30)


def _dt(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


# --- URL + parsing -----------------------------------------------------------

def test_build_url_keeps_semicolons_and_ddmmyyyy_range():
    url = build_url("https://x.test/api.svc/v2/event-ta-date", D29)
    assert url == (
        "https://x.test/api.svc/v2/event-ta-date"
        "?action=get;date-range=29102026000000-29102026235959;format=json"
    )


def test_request_sent_with_raw_semicolons_and_basic_auth():
    fake = FakeMatrix()
    fake.client().fetch_day_raw(D29)
    request = fake.requests[0]
    assert "action=get;date-range=29102026000000-29102026235959;format=json" in str(request.url)
    assert "%3B" not in str(request.url)
    assert request.headers["authorization"].startswith("Basic ")


def test_parses_both_datetime_formats():
    assert parse_event_datetime("05/11/2026 18:33:44") == datetime(2026, 11, 5, 18, 33, 44)
    assert parse_received_at("11/05/2026 09:10:29") == datetime(2026, 11, 5, 9, 10, 29)
    punch = parse_record({
        "userid": "STINT0007", "username": "Gokul D", "eventdatetime": "28/10/2026 18:33:44",
        "entryexittype": "0", "device_name": "New Office", "idatetime": "10/29/2026 09:10:29",
    })
    assert punch.event_datetime == datetime(2026, 10, 28, 18, 33, 44)
    assert punch.event_date == D28
    assert punch.received_at == datetime(2026, 10, 29, 9, 10, 29)
    assert punch.employee_code == "STINT0007"


def test_empty_day_returns_no_records():
    assert FakeMatrix().client().fetch_day_raw(D30) == []


def test_missing_list_key_is_empty():
    fake = FakeMatrix()
    fake.raw_body = "{}"
    assert fake.client().fetch_day_raw(D30) == []


@pytest.mark.parametrize("status_code", [401, 403, 500])
def test_http_errors_raise(status_code):
    fake = FakeMatrix()
    fake.fail_with = status_code
    with pytest.raises(MatrixAPIError):
        fake.client().fetch_day_raw(D29)


def test_bad_json_raises():
    fake = FakeMatrix()
    fake.raw_body = "<html>oops</html>"
    with pytest.raises(MatrixAPIError):
        fake.client().fetch_day_raw(D29)


def test_network_error_raises():
    def boom(request):
        raise httpx.ConnectError("down", request=request)

    from app.services.attendance.matrix_client import MatrixClient
    client = MatrixClient(base_url="https://x.test", username="u", password="p", transport=httpx.MockTransport(boom))
    with pytest.raises(MatrixAPIError):
        client.fetch_day_raw(D29)


def test_parse_sync_time():
    assert parse_sync_time("23:30") == (23, 30)
    with pytest.raises(ValueError):
        parse_sync_time("25:00")


# --- daily rules -------------------------------------------------------------

def test_first_in_last_out():
    result = compute_daily([
        (_dt("2026-10-29 09:05"), "0"),
        (_dt("2026-10-29 13:00"), "1"),
        (_dt("2026-10-29 09:30"), "0"),
        (_dt("2026-10-29 18:10"), "1"),
    ])
    assert result["punch_in"] == _dt("2026-10-29 09:05") and result["punch_in_source"] == "IN"
    assert result["punch_out"] == _dt("2026-10-29 18:10") and result["punch_out_source"] == "OUT"
    assert result["worked_minutes"] == 545
    assert result["punch_count"] == 4


def test_no_out_falls_back_to_last_punch():
    result = compute_daily([(_dt("2026-10-29 09:00"), "0"), (_dt("2026-10-29 18:00"), "0")])
    assert result["punch_out"] == _dt("2026-10-29 18:00")
    assert result["punch_out_source"] == "FALLBACK_LAST_PUNCH"
    assert result["worked_minutes"] == 540


def test_no_in_falls_back_to_first_punch():
    result = compute_daily([(_dt("2026-10-29 09:00"), "1"), (_dt("2026-10-29 18:00"), "1")])
    assert result["punch_in"] == _dt("2026-10-29 09:00")
    assert result["punch_in_source"] == "FALLBACK_FIRST_PUNCH"
    assert result["punch_out"] == _dt("2026-10-29 18:00") and result["punch_out_source"] == "OUT"


def test_single_punch_has_no_out():
    result = compute_daily([(_dt("2026-10-29 09:00"), "0")])
    assert result["punch_in"] == _dt("2026-10-29 09:00") and result["punch_in_source"] == "IN"
    assert result["punch_out"] is None and result["punch_out_source"] is None
    assert result["worked_minutes"] is None
    assert result["punch_count"] == 1


def test_out_before_in_gives_no_worked_minutes():
    result = compute_daily([(_dt("2026-10-29 08:00"), "1"), (_dt("2026-10-29 09:00"), "0")])
    assert result["punch_out"] == _dt("2026-10-29 08:00")
    assert result["worked_minutes"] is None


# --- sync --------------------------------------------------------------------

def _fixture():
    return FakeMatrix({
        D29: [
            # Late-synced: punched 28-Oct evening, received 29-Oct morning.
            rec("STINT0007", "28/10/2026 09:00:00", "0", "New Office", received="10/29/2026 09:10:29"),
            rec("STINT0007", "28/10/2026 18:33:44", "0", "New Office", received="10/29/2026 09:10:29"),
            rec("STEMP0192", "29/10/2026 09:01:00", "0"),
            rec("STEMP0192", "29/10/2026 18:30:00", "1"),
            rec("STEMP0050", "29/10/2026 10:00:00", "0", "New Office"),
        ],
        D30: [
            rec("STEMP0192", "30/10/2026 09:15:00", "0"),
            rec("STEMP0192", "30/10/2026 12:00:00", "0", "New Office"),
        ],
    })


def _daily(db, code, day):
    return db.scalar(select(AttendanceDaily).where(AttendanceDaily.employee_code == code, AttendanceDaily.work_date == day))


def test_sync_groups_by_event_date_and_applies_rules(db_session):
    fake = _fixture()
    run = sync_attendance(db_session, D29, D30, triggered_by="manual", client=fake.client())
    assert run.status == "success"
    assert run.records_fetched == 7
    assert run.records_new == 7
    assert run.days_recomputed == 4
    assert run.finished_at is not None
    assert len(fake.requests) == 2  # one request per day

    late = _daily(db_session, "STINT0007", D28)
    assert late is not None, "late-synced punch must land on its real date"
    assert _daily(db_session, "STINT0007", D29) is None
    assert late.punch_out_source == "FALLBACK_LAST_PUNCH"
    assert late.punch_out == datetime(2026, 10, 28, 18, 33, 44)

    normal = _daily(db_session, "STEMP0192", D29)
    assert normal.punch_in_source == "IN" and normal.punch_out_source == "OUT"
    assert normal.worked_minutes == 569

    single = _daily(db_session, "STEMP0050", D29)
    assert single.punch_out is None and single.punch_count == 1

    punch = db_session.scalar(select(AttendancePunch).where(AttendancePunch.employee_code == "STINT0007").limit(1))
    assert punch.received_at == datetime(2026, 10, 29, 9, 10, 29)
    assert punch.event_date == D28


def test_resync_is_idempotent(db_session):
    fake = _fixture()
    sync_attendance(db_session, D29, D30, client=fake.client())
    before = [
        (d.employee_code, d.work_date, d.punch_in, d.punch_out, d.punch_out_source, d.worked_minutes, d.punch_count)
        for d in db_session.scalars(select(AttendanceDaily).order_by(AttendanceDaily.id))
    ]
    run2 = sync_attendance(db_session, D29, D30, client=fake.client())
    assert run2.status == "success"
    assert run2.records_fetched == 7 and run2.records_new == 0
    assert db_session.scalar(select(func.count()).select_from(AttendancePunch)) == 7
    after = [
        (d.employee_code, d.work_date, d.punch_in, d.punch_out, d.punch_out_source, d.worked_minutes, d.punch_count)
        for d in db_session.scalars(select(AttendanceDaily).order_by(AttendanceDaily.id))
    ]
    assert before == after


def test_late_punch_added_in_later_sync_updates_existing_day(db_session):
    fake = FakeMatrix({D29: [rec("STEMP0192", "29/10/2026 09:00:00", "0")]})
    sync_attendance(db_session, D29, D29, client=fake.client())
    assert _daily(db_session, "STEMP0192", D29).punch_out is None
    fake.by_day[D30] = [rec("STEMP0192", "29/10/2026 18:00:00", "1", received="10/30/2026 08:00:00")]
    sync_attendance(db_session, D29, D30, client=fake.client())
    day = _daily(db_session, "STEMP0192", D29)
    assert day.punch_out == datetime(2026, 10, 29, 18, 0) and day.punch_out_source == "OUT"
    assert day.punch_count == 2


def test_unmatched_then_matched_after_employee_created(db_session):
    fake = FakeMatrix({D29: [rec("STEMP0192", "29/10/2026 09:00:00", "0"), rec("STEMP0192", "29/10/2026 18:00:00", "1")]})
    sync_attendance(db_session, D29, D29, client=fake.client())
    assert _daily(db_session, "STEMP0192", D29).employee_id is None

    emp = Employee(employee_code="ST-EMP-192", first_name="Kaviarasan", last_name="B")
    db_session.add(emp)
    db_session.commit()

    assert relink_employees(db_session) == 1
    db_session.commit()
    assert _daily(db_session, "STEMP0192", D29).employee_id == emp.id
    assert all(p.employee_id == emp.id for p in db_session.scalars(select(AttendancePunch)))


def test_matched_at_sync_time_when_employee_exists(db_session):
    emp = Employee(employee_code="STEMP0192", first_name="Kaviarasan")
    db_session.add(emp)
    db_session.commit()
    fake = FakeMatrix({D29: [rec("ST-EMP-192", "29/10/2026 09:00:00", "0")]})
    sync_attendance(db_session, D29, D29, client=fake.client())
    punch = db_session.scalar(select(AttendancePunch))
    assert punch.raw_userid == "ST-EMP-192" and punch.employee_code == "STEMP0192"
    assert punch.employee_id == emp.id


def test_api_failure_marks_run_failed_and_writes_nothing(db_session):
    fake = _fixture()
    fake.fail_with = 401
    run = sync_attendance(db_session, D29, D30, client=fake.client())
    assert run.status == "failed"
    assert "credentials" in run.error_message
    assert "secret" not in run.error_message
    assert run.records_new == 0 and run.finished_at is not None
    assert db_session.scalar(select(func.count()).select_from(AttendancePunch)) == 0
    assert db_session.scalar(select(func.count()).select_from(AttendanceSyncRun)) == 1


def test_bad_record_is_skipped_not_fatal(db_session):
    fake = FakeMatrix({D29: [
        rec("STEMP0192", "29/10/2026 09:00:00", "0"),
        {"userid": "STEMP0001", "eventdatetime": "garbage", "entryexittype": "0", "device_name": "X", "idatetime": ""},
    ]})
    run = sync_attendance(db_session, D29, D29, client=fake.client())
    assert run.status == "success"
    assert run.records_fetched == 2 and run.records_new == 1
    assert "Skipped 1" in run.error_message
