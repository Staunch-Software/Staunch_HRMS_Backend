"""Mobile (app) punches: validation matrix, merge with Matrix punches, device
tokens and HR review."""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.attendance import MOBILE_DEVICE_NAME, AttendanceDaily, AttendancePunch
from app.models.attendance_month import AttendanceMonthResult
from app.models.mobile_attendance import MobilePunch, OfficeLocation
from app.models.variable_pay import VariablePayInput
from app.services.attendance import mobile
from app.services.attendance.sync import recompute_daily
from tests.leave_helpers import API, bearer, make_employee, make_location, make_user, token_for

UTC = timezone.utc
# 09:30 IST on Tuesday 10 Nov 2026.
NOW = datetime(2026, 11, 10, 4, 0, tzinfo=UTC)
DAY = date(2026, 11, 10)
OFFICE = (13.0418, 80.2341)
# About 55 m north / 550 m north of the office centre.
NEAR = (13.0423, 80.2341)
FAR = (13.0468, 80.2341)
BSSID = "aa:bb:cc:dd:ee:01"


def _office(db, location=None, radius=100, bssids=(BSSID,)):
    office = OfficeLocation(name="Main Door building", work_location_id=location.id if location else None,
                            latitude=Decimal(str(OFFICE[0])), longitude=Decimal(str(OFFICE[1])), radius_m=radius,
                            wifi_bssids=list(bssids), active=True)
    db.add(office)
    db.commit()
    return office


def _item(point=NEAR, kind="IN", at=NOW, accuracy=20.0, **extra):
    values = dict(client_uuid=str(uuid.uuid4()), punch_type=kind, device_time=at, trusted_time=at,
                  latitude=point[0] if point else None, longitude=point[1] if point else None, accuracy_m=accuracy)
    values.update(extra)
    return mobile.PunchInput(**values)


def _submit(db, employee, item, now=NOW, source="APP"):
    return mobile.submit_punches(db, employee, None, [item], source, now=now)[0]


def _setup(db):
    location = make_location(db)
    employee = make_employee(db, "STEMP0101", first="Asha", location=location)
    office = _office(db, location)
    return employee, office


def test_haversine_and_bssid_normalisation():
    assert 50 < mobile.haversine_m(*OFFICE, *NEAR) < 60
    assert 540 < mobile.haversine_m(*OFFICE, *FAR) < 570
    assert mobile.normalize_bssid("AA-BB-CC-DD-EE-01") == BSSID
    assert mobile.normalize_bssid("02:00:00:00:00:00") is None


def test_inside_radius_is_accepted_and_builds_the_day(db_session):
    employee, office = _setup(db_session)
    result = _submit(db_session, employee, _item())
    punch = result.punch
    assert punch.status == "ACCEPTED" and punch.flags == []
    assert punch.office_location_id == office.id and 50 < punch.distance_m < 60
    assert punch.punch_time == datetime(2026, 11, 10, 9, 30)
    row = db_session.scalar(select(AttendancePunch))
    assert (row.device_name, row.source, row.mobile_punch_id, row.entry_exit_type) == (
        MOBILE_DEVICE_NAME, "MOBILE", punch.id, "0")
    daily = db_session.scalar(select(AttendanceDaily))
    assert daily.punch_in == datetime(2026, 11, 10, 9, 30) and daily.entry_device == MOBILE_DEVICE_NAME


def test_outside_radius_is_rejected(db_session):
    employee, _ = _setup(db_session)
    punch = _submit(db_session, employee, _item(point=FAR)).punch
    assert punch.status == "REJECTED" and punch.flags == ["OUTSIDE_RADIUS"]
    assert db_session.scalar(select(AttendancePunch)) is None


def test_no_fix_and_no_office_wifi_is_rejected(db_session):
    employee, _ = _setup(db_session)
    assert _submit(db_session, employee, _item(point=None)).punch.status == "REJECTED"


def test_mock_location_is_flagged(db_session):
    employee, _ = _setup(db_session)
    punch = _submit(db_session, employee, _item(is_mock=True)).punch
    assert punch.status == "FLAGGED" and "MOCK_LOCATION" in punch.flags
    assert db_session.scalar(select(AttendancePunch)) is None


def test_poor_accuracy_is_flagged(db_session):
    employee, _ = _setup(db_session)
    punch = _submit(db_session, employee, _item(accuracy=250)).punch
    assert punch.status == "FLAGGED" and punch.flags == ["POOR_ACCURACY"]


def test_office_wifi_with_poor_gps_is_accepted(db_session):
    employee, office = _setup(db_session)
    punch = _submit(db_session, employee, _item(point=FAR, accuracy=900, wifi_bssid=BSSID)).punch
    assert punch.status == "ACCEPTED" and punch.wifi_match and punch.office_location_id == office.id
    assert db_session.scalar(select(AttendancePunch)) is not None


def test_office_wifi_with_confident_far_gps_is_flagged(db_session):
    employee, _ = _setup(db_session)
    punch = _submit(db_session, employee, _item(point=FAR, accuracy=10, wifi_bssid=BSSID)).punch
    assert punch.status == "FLAGGED" and punch.flags == ["LOCATION_CONFLICT"]


def test_unknown_wifi_outside_radius_is_still_rejected(db_session):
    employee, _ = _setup(db_session)
    punch = _submit(db_session, employee, _item(point=FAR, wifi_bssid="aa:bb:cc:dd:ee:99")).punch
    assert punch.status == "REJECTED" and not punch.wifi_match


def test_clock_suspect_is_flagged(db_session):
    employee, _ = _setup(db_session)
    rebooted = _submit(db_session, employee, _item(clock_suspect=True)).punch
    assert rebooted.status == "FLAGGED" and rebooted.flags == ["CLOCK_SUSPECT"] and rebooted.clock_suspect
    skewed = _item()
    skewed.device_time = NOW - timedelta(minutes=20)
    assert _submit(db_session, employee, skewed).punch.flags == ["CLOCK_SUSPECT"]
    no_reference = _item(trusted_time=None)
    assert _submit(db_session, employee, no_reference).punch.flags == ["CLOCK_SUSPECT"]
    online_but_late = _item(at=NOW - timedelta(minutes=30))
    assert _submit(db_session, employee, online_but_late).punch.flags == ["CLOCK_SUSPECT"]
    queued = _item(at=NOW - timedelta(minutes=30), captured_offline=True)
    assert _submit(db_session, employee, queued).punch.status == "ACCEPTED"


def test_duplicate_client_uuid_returns_the_stored_outcome(db_session):
    employee, _ = _setup(db_session)
    item = _item()
    first = _submit(db_session, employee, item)
    again = mobile.submit_punches(db_session, employee, None, [item, item], "APP", now=NOW + timedelta(hours=1))
    assert [r.duplicate for r in again] == [True, True]
    assert {r.punch.id for r in again} == {first.punch.id}
    assert len(db_session.scalars(select(MobilePunch)).all()) == 1
    assert len(db_session.scalars(select(AttendancePunch)).all()) == 1


def test_upload_after_the_age_limit_is_flagged(db_session):
    employee, _ = _setup(db_session)
    old = _item(at=NOW - timedelta(days=8), captured_offline=True)
    punch = _submit(db_session, employee, old).punch
    assert punch.status == "FLAGGED" and punch.flags == ["TOO_OLD"]
    recent = _item(at=NOW - timedelta(days=6), captured_offline=True)
    assert _submit(db_session, employee, recent).punch.status == "ACCEPTED"


def test_month_only_sent_to_payroll_still_accepts(db_session):
    employee, _ = _setup(db_session)
    db_session.add(AttendanceMonthResult(employee_id=employee.id, month="2026-11", status="SENT",
                                         sent_at=NOW, totals={}, days=[], warnings=[]))
    db_session.add(VariablePayInput(employee_id=employee.id, period_start=date(2026, 11, 1),
                                    period_end=date(2026, 11, 30), source="Attendance", status="Draft"))
    db_session.commit()
    assert _submit(db_session, employee, _item()).punch.status == "ACCEPTED"


def test_locked_payroll_month_is_flagged(db_session):
    employee, _ = _setup(db_session)
    db_session.add(VariablePayInput(employee_id=employee.id, period_start=date(2026, 11, 1),
                                    period_end=date(2026, 11, 30), source="Attendance", status="Locked"))
    db_session.commit()
    punch = _submit(db_session, employee, _item()).punch
    assert punch.status == "FLAGGED" and punch.flags == ["LOCKED_MONTH"]


def test_office_of_another_work_location_does_not_count(db_session):
    employee, _ = _setup(db_session)
    other = make_location(db_session, name="Mumbai", state_code="MH")
    db_session.query(OfficeLocation).update({OfficeLocation.work_location_id: other.id})
    db_session.commit()
    punch = _submit(db_session, employee, _item()).punch
    assert punch.status == "REJECTED" and punch.flags == ["NO_OFFICE"]


def test_mobile_punches_merge_with_matrix_earliest_in_latest_out(db_session):
    employee, _ = _setup(db_session)
    db_session.add(AttendancePunch(employee_code="STEMP0101", raw_userid="STEMP0101", event_datetime=datetime(
        2026, 11, 10, 9, 40), event_date=DAY, entry_exit_type="0", device_name="Main Door", employee_id=employee.id,
        source="MATRIX"))
    db_session.add(AttendancePunch(employee_code="STEMP0101", raw_userid="STEMP0101", event_datetime=datetime(
        2026, 11, 10, 18, 0), event_date=DAY, entry_exit_type="1", device_name="Main Door", employee_id=employee.id,
        source="MATRIX"))
    db_session.commit()
    recompute_daily(db_session, [("STEMP0101", DAY)], {"STEMP0101": employee.id})
    db_session.commit()
    _submit(db_session, employee, _item(at=datetime(2026, 11, 10, 3, 50, tzinfo=UTC)),
            now=datetime(2026, 11, 10, 3, 51, tzinfo=UTC))
    evening = datetime(2026, 11, 10, 13, 15, tzinfo=UTC)
    _submit(db_session, employee, _item(kind="OUT", at=evening), now=evening)
    daily = db_session.scalar(select(AttendanceDaily))
    assert daily.punch_in == datetime(2026, 11, 10, 9, 20) and daily.entry_device == MOBILE_DEVICE_NAME
    assert daily.punch_out == datetime(2026, 11, 10, 18, 45) and daily.exit_device == MOBILE_DEVICE_NAME
    assert daily.punch_count == 4


# --- API ---------------------------------------------------------------------

def _employee_login(client, db):
    employee, office = _setup(db)
    make_user(db, "asha", employee=employee)
    return employee, office, token_for(client, "asha")


def _payload(point=NEAR, **extra):
    now = datetime.now(UTC)
    body = dict(client_uuid=str(uuid.uuid4()), punch_type="IN", device_time=now.isoformat(),
                trusted_time=now.isoformat(), latitude=point[0], longitude=point[1], accuracy_m=15)
    body.update(extra)
    return {"punches": [body]}


def _register(client, token, device_uuid="phone-0001"):
    response = client.post(f"{API}/me/mobile-devices/register", headers=bearer(token),
                           json={"device_uuid": device_uuid, "model": "Pixel 7", "platform": "android"})
    return response


def test_device_token_uploads_without_a_session(client, db_session):
    _, office, token = _employee_login(client, db_session)
    registered = _register(client, token)
    assert registered.status_code == 200, registered.text
    device_token = registered.json()["device_token"]

    config = client.get(f"{API}/me/mobile-config", headers={"Authorization": "", "X-Device-Token": device_token})
    assert config.status_code == 200, config.text
    body = config.json()
    assert body["device_registered"] and body["offices"][0]["id"] == office.id
    assert body["offices"][0]["wifi_bssids"] == [BSSID] and body["rules"]["max_accuracy_m"] == 100

    response = client.post(f"{API}/me/mobile-punches", json=_payload(),
                           headers={"Authorization": "Bearer expired.or.bad", "X-Device-Token": device_token})
    assert response.status_code == 200, response.text
    assert response.json()[0]["status"] == "ACCEPTED"
    stored = db_session.scalar(select(MobilePunch))
    assert stored.device_id == registered.json()["device_id"] and stored.source == "APP"

    mine = client.get(f"{API}/me/mobile-punches", headers={"X-Device-Token": device_token, "Authorization": ""})
    assert [p["status"] for p in mine.json()] == ["ACCEPTED"]

    bad = client.post(f"{API}/me/mobile-punches", json=_payload(),
                      headers={"Authorization": "", "X-Device-Token": device_token + "x"})
    assert bad.status_code == 401


def test_one_phone_per_employee_until_hr_resets(client, db_session):
    employee, _, token = _employee_login(client, db_session)
    first = _register(client, token).json()["device_token"]
    assert _register(client, token).status_code == 200
    assert _register(client, token, device_uuid="phone-0002").status_code == 409

    reset = client.post(f"{API}/mobile-devices/{employee.id}/reset")
    assert reset.status_code == 200 and reset.json()["revoked_by"] == "admin"
    revoked = client.post(f"{API}/me/mobile-punches", json=_payload(),
                          headers={"Authorization": "", "X-Device-Token": first})
    assert revoked.status_code == 401
    assert _register(client, token, device_uuid="phone-0002").status_code == 200
    devices = client.get(f"{API}/mobile-devices").json()
    assert [d["device_uuid"] for d in devices] == ["phone-0002"]


def test_a_phone_serves_one_employee(client, db_session):
    _, _, token = _employee_login(client, db_session)
    assert _register(client, token).status_code == 200
    other = make_employee(db_session, "STEMP0102", first="Bala")
    make_user(db_session, "bala", employee=other)
    taken = _register(client, token_for(client, "bala"))
    assert taken.status_code == 409 and "another employee" in taken.json()["detail"]


def test_browser_punch_needs_the_setting_and_is_always_flagged(client, db_session):
    _, _, token = _employee_login(client, db_session)
    assert client.post(f"{API}/me/mobile-punches", json=_payload(), headers=bearer(token)).status_code == 403
    settings = client.put(f"{API}/mobile-attendance/settings", json={
        "max_upload_age_days": 7, "max_accuracy_m": 100, "clock_tolerance_minutes": 5, "allow_web_punch": True})
    assert settings.status_code == 200, settings.text
    response = client.post(f"{API}/me/mobile-punches", json=_payload(), headers=bearer(token))
    assert response.status_code == 200 and response.json()[0]["status"] == "FLAGGED"
    assert response.json()[0]["flags"] == ["BROWSER"]


def test_hr_approves_flagged_and_rejects_accepted(client, db_session):
    _, _, token = _employee_login(client, db_session)
    device_token = _register(client, token).json()["device_token"]
    headers = {"X-Device-Token": device_token}
    flagged = client.post(f"{API}/me/mobile-punches", json=_payload(is_mock=True), headers=headers).json()[0]
    assert flagged["status"] == "FLAGGED"
    queue = client.get(f"{API}/mobile-punches", params={"status": "FLAGGED"}).json()
    assert [p["id"] for p in queue] == [flagged["id"]] and queue[0]["is_mock"]
    assert client.get(f"{API}/mobile-punches/counts").json()["flagged"] == 1

    approved = client.post(f"{API}/mobile-punches/{flagged['id']}/approve", json={"note": "Checked CCTV"})
    assert approved.status_code == 200 and approved.json()["status"] == "ACCEPTED"
    assert approved.json()["review_action"] == "APPROVED" and approved.json()["reviewed_by"] == "admin"
    assert db_session.scalar(select(AttendancePunch).where(AttendancePunch.mobile_punch_id == flagged["id"]))
    assert db_session.scalar(select(AttendanceDaily)).punch_in is not None

    rejected = client.post(f"{API}/mobile-punches/{flagged['id']}/reject", json={})
    assert rejected.status_code == 200 and rejected.json()["status"] == "REJECTED"
    db_session.expire_all()
    assert db_session.scalar(select(AttendancePunch)) is None
    assert db_session.scalar(select(AttendanceDaily)) is None


def test_office_location_crud(client, db_session):
    location = make_location(db_session)
    created = client.post(f"{API}/office-locations", json={
        "name": " New Office ", "work_location_id": location.id, "latitude": 13.0418, "longitude": 80.2341,
        "wifi_bssids": ["AA-BB-CC-DD-EE-02", "aabbccddee02", ""]})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "New Office" and body["radius_m"] == 100 and body["wifi_bssids"] == ["aa:bb:cc:dd:ee:02"]
    assert body["work_location_name"] == "Chennai"
    bad = client.post(f"{API}/office-locations", json={"name": "X", "latitude": 13, "longitude": 80,
                                                       "wifi_bssids": ["not-a-mac"]})
    assert bad.status_code == 422
    updated = client.put(f"{API}/office-locations/{body['id']}", json={**body, "radius_m": 150})
    assert updated.status_code == 200 and updated.json()["radius_m"] == 150
    assert len(client.get(f"{API}/office-locations").json()) == 1
    assert client.delete(f"{API}/office-locations/{body['id']}").status_code == 200
    assert client.get(f"{API}/office-locations").json() == []


def test_employee_cannot_use_hr_endpoints(client, db_session):
    _, _, token = _employee_login(client, db_session)
    assert client.get(f"{API}/mobile-punches", headers=bearer(token)).status_code == 403
    assert client.get(f"{API}/office-locations", headers=bearer(token)).status_code == 403


def test_location_test_is_a_dry_run(client, db_session):
    employee, office = _setup(db_session)
    near = client.post(f"{API}/office-locations/test", json={"latitude": NEAR[0], "longitude": NEAR[1]}).json()
    assert near["offices"][0]["inside"] is True and near["status"] is None
    assert 50 < near["offices"][0]["distance_m"] < 60
    far = client.post(f"{API}/office-locations/test", json={
        "latitude": FAR[0], "longitude": FAR[1], "employee_id": employee.id}).json()
    assert far["offices"][0]["inside"] is False and far["offices"][0]["applies_to_employee"] is True
    assert far["status"] == "REJECTED"
    wifi = client.post(f"{API}/office-locations/test", json={
        "latitude": NEAR[0], "longitude": NEAR[1], "employee_id": employee.id, "wifi_bssid": BSSID}).json()
    assert wifi["status"] == "ACCEPTED"
    assert db_session.scalar(select(MobilePunch.id)) is None
