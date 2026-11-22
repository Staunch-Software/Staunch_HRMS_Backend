"""Sync coverage (fully covered days, stale flag, catch-up range) and the
late-upload hold in monthly processing."""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models.attendance import AttendanceSyncRun
from app.services.attendance import monthly
from app.services.attendance.coverage import (
    MAX_CATCHUP_DAYS,
    catch_up,
    chunk_range,
    coverage_status,
    uncovered_dates,
)
from app.services.leave.calendar import ensure_default_policy
from tests.attendance_fakes import FakeMatrix
from tests.leave_helpers import API, make_employee, seed_types
from tests.test_attendance_monthly import full_month, on, punch

TODAY = date(2026, 11, 20)


def local(day: date, hhmm: str) -> datetime:
    """A server-local wall time stored the way the sync stores it (UTC aware)."""
    h, m = hhmm.split(":")
    return datetime(day.year, day.month, day.day, int(h), int(m)).astimezone(timezone.utc)


def run(db, date_from, date_to, finished_day, finished_hhmm="23:31", status="success", triggered_by="scheduler"):
    finished = local(finished_day, finished_hhmm)
    row = AttendanceSyncRun(triggered_by=triggered_by, date_from=date_from, date_to=date_to, status=status,
                            started_at=finished - timedelta(minutes=1), finished_at=finished)
    db.add(row)
    db.commit()
    return row


def nightly(db, day, lookback=3):
    return run(db, day - timedelta(days=lookback), day, day)


def days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


# --- coverage -------------------------------------------------------------------------

def test_nothing_synced_is_stale_and_catches_up_lookback(db_session):
    status = coverage_status(db_session, TODAY)
    assert status.covered_through is None and status.stale
    assert status.sync_to == TODAY and status.sync_from <= TODAY
    assert uncovered_dates(db_session, date(2026, 11, 1), date(2026, 11, 30), TODAY) == []


def test_nightly_runs_cover_through_the_day_before_yesterday(db_session):
    for day in days(date(2026, 11, 10), TODAY - timedelta(days=1)):
        nightly(db_session, day)
    status = coverage_status(db_session, TODAY)
    assert status.covered_through == date(2026, 11, 18)
    assert not status.stale
    assert status.sync_from == date(2026, 11, 17) and status.sync_to == TODAY


def test_missed_nights_make_it_stale_and_catch_up_from_the_gap(db_session):
    for day in days(date(2026, 11, 10), date(2026, 11, 15)):
        nightly(db_session, day)
    status = coverage_status(db_session, TODAY)
    assert status.covered_through == date(2026, 11, 14)
    assert status.stale
    assert status.missing_from == date(2026, 11, 15)
    assert status.missing_dates == days(date(2026, 11, 15), date(2026, 11, 19))
    assert (status.sync_from, status.sync_to) == (date(2026, 11, 13), TODAY)


def test_mid_day_manual_run_is_partial(db_session):
    for day in days(date(2026, 11, 10), date(2026, 11, 15)):
        nightly(db_session, day)
    run(db_session, date(2026, 11, 15), date(2026, 11, 20), TODAY, "11:42", triggered_by="manual")
    status = coverage_status(db_session, TODAY)
    assert status.covered_through == date(2026, 11, 19)
    assert not status.stale


def test_run_on_the_day_only_does_not_cover_it(db_session):
    for day in days(date(2026, 11, 10), date(2026, 11, 15)):
        nightly(db_session, day)
    run(db_session, date(2026, 11, 15), date(2026, 11, 16), date(2026, 11, 16), "12:00", triggered_by="manual")
    status = coverage_status(db_session, TODAY)
    assert status.covered_through == date(2026, 11, 15)
    assert date(2026, 11, 16) in status.missing_dates
    assert status.stale


def test_failed_run_covers_nothing(db_session):
    for day in days(date(2026, 11, 10), date(2026, 11, 17)):
        nightly(db_session, day)
    run(db_session, date(2026, 11, 15), date(2026, 11, 19), date(2026, 11, 19), status="failed")
    status = coverage_status(db_session, TODAY)
    assert status.covered_through == date(2026, 11, 16)
    assert status.stale


def test_old_hole_is_reported_and_month_gaps_listed(db_session):
    nightly(db_session, date(2026, 11, 3))
    run(db_session, date(2026, 11, 5), date(2026, 11, 19), TODAY, "10:00", triggered_by="manual")
    status = coverage_status(db_session, TODAY)
    assert status.covered_through == date(2026, 11, 2)
    assert status.missing_dates == [date(2026, 11, 3), date(2026, 11, 4)]
    assert uncovered_dates(db_session, date(2026, 11, 1), date(2026, 11, 30), TODAY) == [
        date(2026, 11, 3), date(2026, 11, 4),
    ]
    assert uncovered_dates(db_session, date(2026, 10, 1), date(2026, 10, 31), TODAY) == []


def test_catch_up_range_is_capped():
    chunks = chunk_range(TODAY - timedelta(days=MAX_CATCHUP_DAYS - 1), TODAY)
    assert len(chunks) == 2
    assert all((end - start).days + 1 == 31 for start, end in chunks)
    assert chunks[-1][1] == TODAY
    assert chunk_range(TODAY - timedelta(days=2), TODAY) == [(TODAY - timedelta(days=2), TODAY)]


def test_long_gap_is_capped_to_max_days(db_session):
    nightly(db_session, TODAY - timedelta(days=200))
    status = coverage_status(db_session, TODAY)
    assert status.stale
    assert status.sync_from == TODAY - timedelta(days=MAX_CATCHUP_DAYS - 1)


def test_catch_up_syncs_the_missing_range_in_chunks(db_session):
    nightly(db_session, TODAY - timedelta(days=100))
    fake = FakeMatrix()
    runs = catch_up(db_session, triggered_by="startup", today=TODAY, client=fake.client())
    assert [r.status for r in runs] == ["success", "success"]
    assert all(r.triggered_by == "startup" for r in runs)
    assert runs[0].date_from == TODAY - timedelta(days=MAX_CATCHUP_DAYS - 1)
    assert runs[-1].date_to == TODAY
    assert runs[0].date_to + timedelta(days=1) == runs[1].date_from
    assert len(fake.requests) == MAX_CATCHUP_DAYS


def test_catch_up_stops_at_a_failed_chunk(db_session):
    nightly(db_session, TODAY - timedelta(days=45))
    fake = FakeMatrix()
    fake.fail_with = 500
    runs = catch_up(db_session, today=TODAY, client=fake.client())
    assert [r.status for r in runs] == ["failed"]


def test_sync_status_api_and_dashboard(client, db_session):
    nightly(db_session, date.today() - timedelta(days=10))
    status = client.get(f"{API}/attendance/sync-status").json()
    assert status["stale"] is True
    assert status["sync_to"] == date.today().isoformat()
    assert status["max_sync_days"] == 31
    overview = client.get(f"{API}/dashboard/overview").json()
    assert overview["attendance_sync"]["stale"] is True


def test_catch_up_api(client, db_session, monkeypatch):
    fake = FakeMatrix()
    from app.services.attendance import sync as sync_module
    monkeypatch.setattr(sync_module, "MatrixClient", lambda: fake.client())
    nightly(db_session, date.today() - timedelta(days=5))
    resp = client.post(f"{API}/attendance/sync/catch-up")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["triggered_by"] for r in body["runs"]] == ["catchup"]
    assert body["runs"][0]["status"] == "success"
    assert body["status"]["covered_through"] == (date.today() - timedelta(days=1)).isoformat()
    assert body["status"]["stale"] is False


# --- late-upload hold -----------------------------------------------------------------

@pytest.fixture()
def emp(db_session):
    seed_types(db_session)
    return make_employee(db_session, "ST0001", joined=date(2025, 1, 1))


@pytest.mark.parametrize("rule", ["PRESENT", "HALF_DAY", "ABSENT"])
def test_missing_out_is_held_while_recent(db_session, emp, rule):
    policy = ensure_default_policy(db_session)
    policy.missing_out_policy = rule
    db_session.commit()
    today = date(2026, 11, 20)
    full_month(db_session, emp, skip={date(2026, 11, 19)}, last=date(2026, 11, 18))
    punch(db_session, emp, date(2026, 11, 19), "09:25", None)
    result = monthly.resolve_month(db_session, emp, "2026-11", today=today)
    day = on(result, date(2026, 11, 19))
    assert monthly.FLAG_AWAITING_UPLOAD in day.flags
    assert monthly.FLAG_MISSING_OUT not in day.flags
    assert day.lop == 0 and day.present == 1
    assert result.totals["awaiting_upload_days"] == 1
    assert result.totals["missing_punch_days"] == 0
    assert any("awaiting device upload" in w for w in result.warnings)


@pytest.mark.parametrize("rule, lop", [("PRESENT", "0"), ("HALF_DAY", "0.5"), ("ABSENT", "1")])
def test_missing_out_is_evaluated_after_the_hold(db_session, emp, rule, lop):
    policy = ensure_default_policy(db_session)
    policy.missing_out_policy = rule
    db_session.commit()
    full_month(db_session, emp, skip={date(2026, 11, 18)}, last=date(2026, 11, 17))
    punch(db_session, emp, date(2026, 11, 18), "09:25", None)
    result = monthly.resolve_month(db_session, emp, "2026-11", today=date(2026, 11, 20))
    day = on(result, date(2026, 11, 18))
    assert monthly.FLAG_AWAITING_UPLOAD not in day.flags
    assert monthly.FLAG_MISSING_OUT in day.flags
    assert day.lop == Decimal(lop)


def test_hold_days_follow_the_policy(db_session, emp):
    policy = ensure_default_policy(db_session)
    policy.missing_out_policy = "ABSENT"
    policy.late_upload_hold_days = 4
    db_session.commit()
    punch(db_session, emp, date(2026, 11, 17), "09:25", None)
    result = monthly.resolve_month(db_session, emp, "2026-11", today=date(2026, 11, 20))
    assert monthly.FLAG_AWAITING_UPLOAD in on(result, date(2026, 11, 17)).flags

    policy.late_upload_hold_days = 0
    db_session.commit()
    result = monthly.resolve_month(db_session, emp, "2026-11", today=date(2026, 11, 20))
    assert on(result, date(2026, 11, 17)).lop == 1


def test_missing_in_is_not_held(db_session, emp):
    policy = ensure_default_policy(db_session)
    policy.missing_out_policy = "ABSENT"
    db_session.commit()
    punch(db_session, emp, date(2026, 11, 19), None, "18:30")
    result = monthly.resolve_month(db_session, emp, "2026-11", today=date(2026, 11, 20))
    day = on(result, date(2026, 11, 19))
    assert monthly.FLAG_AWAITING_UPLOAD not in day.flags and day.lop == 1


def test_unsettled_from():
    policy = type("P", (), {"late_upload_hold_days": 2})()
    assert monthly.unsettled_from(date(2026, 11, 30), policy, date(2026, 11, 20)) == date(2026, 11, 19)
    assert monthly.unsettled_from(date(2026, 10, 31), policy, date(2026, 11, 1)) == date(2026, 10, 31)
    assert monthly.unsettled_from(date(2026, 10, 31), policy, date(2026, 11, 2)) is None
    policy.late_upload_hold_days = 0
    assert monthly.unsettled_from(date(2026, 11, 30), policy, date(2026, 11, 20)) is None


def test_policy_api_round_trips_hold_days(client, db_session):
    current = client.get(f"{API}/attendance-settings/policy").json()
    body = current["default"] if "default" in current else current
    assert body["late_upload_hold_days"] == 2


def test_monthly_listing_reports_sync_gaps(client, db_session):
    from tests.test_attendance_monthly import _groups

    chennai = _groups(db_session)["CHENNAI_HO"]
    first = (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
    last = date.today().replace(day=1) - timedelta(days=1)
    gap = first + timedelta(days=9)
    run(db_session, first, gap - timedelta(days=1), date.today(), "00:30", triggered_by="manual")
    run(db_session, gap + timedelta(days=1), last, date.today(), "00:30", triggered_by="manual")
    body = client.get(f"{API}/attendance/monthly",
                      params={"month": first.strftime("%Y-%m"), "pay_group_id": chennai.id}).json()
    assert body["uncovered_sync_dates"] == [gap.isoformat()]
    assert body["late_upload_hold_days"] == 2
    assert any("not fully covered" in w for w in body["warnings"])
