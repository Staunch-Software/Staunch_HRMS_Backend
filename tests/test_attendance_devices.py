"""Entry / exit per Matrix device (IN_OUT vs SINGLE readers), device settings
and recomputing daily rows after a mode change."""
from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.models.attendance import AttendanceDaily, AttendanceDevice
from app.services.attendance import monthly
from app.services.attendance.sync import compute_daily, sync_attendance
from tests.attendance_fakes import FakeMatrix, rec
from tests.leave_helpers import API, make_employee, seed_types

NOV10 = date(2026, 11, 10)  # a Tuesday


def _dt(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, datetime.strptime(hhmm, "%H:%M").time())


def _ev(day: date, hhmm: str) -> str:
    return f"{day:%d/%m/%Y} {hhmm}:00"


def _sync(db, day, records):
    run = sync_attendance(db, day, day, client=FakeMatrix({day: records}).client())
    assert run.status == "success", run.error_message
    return db.scalar(select(AttendanceDaily).where(AttendanceDaily.work_date == day))


def test_main_door_then_new_office_takes_last_new_office_punch_as_exit():
    result = compute_daily([
        (_dt(NOV10, "09:20"), "0", "Main Door"),
        (_dt(NOV10, "13:00"), "1", "Main Door"),
        (_dt(NOV10, "14:00"), "0", "New Office"),
        (_dt(NOV10, "18:40"), "0", "New Office"),
    ])
    assert result["punch_in"] == _dt(NOV10, "09:20") and result["punch_in_source"] == "IN"
    assert result["punch_out"] == _dt(NOV10, "18:40") and result["punch_out_source"] == "SINGLE_LAST_PUNCH"
    assert (result["entry_device"], result["exit_device"]) == ("Main Door", "New Office")
    assert result["worked_minutes"] == 560 and result["punch_count"] == 4


def test_earliest_entry_across_devices_and_unknown_device_is_in_out():
    result = compute_daily([
        (_dt(NOV10, "09:05"), "0", "New Office"),
        (_dt(NOV10, "09:30"), "0", "Main Door"),
        (_dt(NOV10, "12:00"), "0", "Gate 3"),
        (_dt(NOV10, "17:00"), "1", "Main Door"),
    ])
    assert result["punch_in"] == _dt(NOV10, "09:05") and result["entry_device"] == "New Office"
    assert result["punch_out"] == _dt(NOV10, "17:00") and result["exit_device"] == "Main Door"


def test_mode_map_overrides_the_default():
    punches = [(_dt(NOV10, "09:00"), "0", "New Office"), (_dt(NOV10, "18:00"), "0", "New Office")]
    assert compute_daily(punches)["punch_out_source"] == "SINGLE_LAST_PUNCH"
    as_in_out = compute_daily(punches, {"New Office": "IN_OUT"})
    assert as_in_out["punch_in_source"] == "IN" and as_in_out["punch_out_source"] == "FALLBACK_LAST_PUNCH"


def test_mobile_app_in_out(db_session):
    daily = _sync(db_session, NOV10, [
        rec("ST0001", _ev(NOV10, "09:10"), "0", ""),
        rec("ST0001", _ev(NOV10, "13:00"), "1", ""),
        rec("ST0001", _ev(NOV10, "14:00"), "0", ""),
        rec("ST0001", _ev(NOV10, "18:20"), "1", ""),
    ])
    assert (daily.punch_in, daily.punch_out) == (_dt(NOV10, "09:10"), _dt(NOV10, "18:20"))
    assert (daily.punch_in_source, daily.punch_out_source) == ("IN", "OUT")
    assert (daily.entry_device, daily.exit_device) == ("", "")


def _month_day(db, emp, day):
    result = monthly.resolve_month(db, emp, "2026-11", today=date(2026, 12, 31))
    return result, next(x for x in result.days if x.date == day)


def test_new_office_only_day_is_late_from_its_first_punch(db_session):
    seed_types(db_session)
    emp = make_employee(db_session, "ST0001", joined=date(2025, 1, 1))
    _sync(db_session, NOV10, [
        rec("ST0001", _ev(NOV10, "09:40"), "0", "New Office"),
        rec("ST0001", _ev(NOV10, "18:30"), "0", "New Office"),
    ])
    _, day = _month_day(db_session, emp, NOV10)
    assert (day.punch_in, day.punch_out) == (_dt(NOV10, "09:40"), _dt(NOV10, "18:30"))
    assert day.late and day.late_minutes == 10 and day.status == monthly.ST_LATE
    assert (day.entry_device, day.exit_device) == ("New Office", "New Office")
    assert day.lop == 0 and day.worked_minutes == 530


def test_single_new_office_punch_is_present_with_missing_exit(db_session):
    seed_types(db_session)
    emp = make_employee(db_session, "ST0001", joined=date(2025, 1, 1))
    daily = _sync(db_session, NOV10, [rec("ST0001", _ev(NOV10, "09:25"), "0", "New Office")])
    assert daily.punch_out is None and daily.exit_device is None and daily.entry_device == "New Office"
    _, day = _month_day(db_session, emp, NOV10)
    assert day.status == monthly.ST_PRESENT and day.lop == 0
    assert monthly.FLAG_MISSING_OUT in day.flags
    assert day.to_json()["entry_device"] == "New Office" and day.to_json()["exit_device"] is None


def test_device_list_includes_seeded_and_seen_devices(client, db_session):
    _sync(db_session, NOV10, [rec("ST0001", _ev(NOV10, "09:25"), "0", "Gate 3")])
    devices = {d["device_name"]: d for d in client.get(f"{API}/attendance-settings/devices").json()}
    assert devices["Main Door"]["mode"] == "IN_OUT" and devices["New Office"]["mode"] == "SINGLE"
    assert devices[""]["display_name"] == "Mobile app" and devices[""]["mode"] == "IN_OUT"
    assert devices["Gate 3"]["mode"] == "IN_OUT"
    bad = client.put(f"{API}/attendance-settings/devices/{devices['Gate 3']['id']}", json={"mode": "BOTH"})
    assert bad.status_code == 400


def test_mode_change_recomputes_recent_days(client, db_session):
    day = date.today() - timedelta(days=3)
    _sync(db_session, day, [
        rec("ST0001", _ev(day, "09:00"), "0", "New Office"),
        rec("ST0001", _ev(day, "18:00"), "0", "New Office"),
    ])
    old = date.today() - timedelta(days=80)
    _sync(db_session, old, [
        rec("ST0001", _ev(old, "09:00"), "0", "New Office"),
        rec("ST0001", _ev(old, "18:00"), "0", "New Office"),
    ])
    device = db_session.scalar(select(AttendanceDevice).where(AttendanceDevice.device_name == "New Office"))
    resp = client.put(f"{API}/attendance-settings/devices/{device.id}", json={"mode": "IN_OUT"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["device"]["mode"] == "IN_OUT" and body["days_recomputed"] == 1
    assert body["recomputed_to"] == date.today().isoformat()

    def row(work_date):
        db_session.expire_all()
        return db_session.scalar(select(AttendanceDaily).where(AttendanceDaily.work_date == work_date))

    assert row(day).punch_out_source == "FALLBACK_LAST_PUNCH"
    assert row(old).punch_out_source == "SINGLE_LAST_PUNCH"  # outside the 62 days

    same = client.put(f"{API}/attendance-settings/devices/{device.id}", json={"mode": "IN_OUT"})
    assert same.json()["days_recomputed"] == 0

    resp = client.post(f"{API}/attendance/recompute", json={"date_from": old.isoformat(), "date_to": old.isoformat()})
    assert resp.status_code == 200 and resp.json()["days_recomputed"] == 1
    assert row(old).punch_out_source == "FALLBACK_LAST_PUNCH"


def test_recompute_range_limits(client):
    too_long = client.post(f"{API}/attendance/recompute", json={"date_from": "2026-01-01", "date_to": "2026-04-30"})
    assert too_long.status_code == 400 and "92" in too_long.json()["detail"]
    backwards = client.post(f"{API}/attendance/recompute", json={"date_from": "2026-02-01", "date_to": "2026-01-01"})
    assert backwards.status_code == 400
